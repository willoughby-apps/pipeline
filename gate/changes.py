"""What a request changes since the last commit Andrew approved (PLAN section
1, 2026-09-19 decision d), read as data by the gate job.

    python3 -m gate changes <requested_dir> [--base <approved_dir>] --json OUT

Both trees are read the gate's way (`checks.walk_files`, `check_project_yml`
through `yamlsafe`, plists with `plistlib`): nothing is built, run or
resolved. The result is a JSON object of **differences only** (the request
job wraps it with the repo, the commits, the diff link and the pictures into
the request issue's `willoughby-changes v1` block):

    files        {"changed": n, "paths": [first 200, path order], "truncated": bool}
    testers      {"friends": {"added": [{email, name}], "removed": [...]},
                  "external": {"added": [email], "removed": [email]},
                  "friend_role": "MARKETING"}
    display_name {"before": str|null, "after": str|null} | null
    version      {"before", "after"} | null        (MARKETING_VERSION)
    build        {"before", "after"} | null        (CURRENT_PROJECT_VERSION)
    permissions  {"added": [{key, text}], "removed": [key], "changed": [{key, before, after}]}
    hosts        {"added": [host], "removed": [host]}
    entitlements {"added": [{key, value}], "removed": [key], "changed": [{key, before, after}]}
    managed      {"changed": [".claude/settings.json" | "CLAUDE.md"]}

Every value is guest-written text: the request job escapes it into the issue,
and nothing here is printed to a log. With no `--base` (no approved commit
yet), every fact of the requested tree counts as added.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import checks, testers_file
from .policy import load_policy

MANAGED_FILES = (".claude/settings.json", "CLAUDE.md")
TESTERS_FILE = "testers.txt"
MAX_PATHS = 200
MAX_HOSTS = 200
MAX_TEXT = 300
SUMMARY_BUNDLE = "com.willoughbytools.summary.only"   # the gate's Context needs one; nothing checks it here
USAGE_RE = re.compile(r"NS\w+UsageDescription")
URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]{1,20}://([^\s/\"'<>()\\`?#,;{}\[\]]+)")
HOST_RE = re.compile(r"[a-z0-9._-]{1,253}|\[[0-9a-f:.]{2,45}\]")


def _cap(value, limit=MAX_TEXT) -> str:
    return str(value if value is not None else "")[:limit]


def _jsonable(value):
    """A plist value as JSON (dates and data as text), capped."""
    if isinstance(value, dict):
        return {str(k)[:100]: _jsonable(v) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value[:50]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return _cap(value)


def hosts_in(text: str) -> set[str]:
    out = set()
    for line in text.split("\n"):
        # The most decoded form holds every host the line names (escapes like
        # Swift's \\u{2E} are what a build turns back into characters).
        for form in checks.host_forms(line)[-1:]:
            for m in URL_RE.finditer(form):
                if form[m.end():].startswith("/DTDs/"):
                    continue  # a property list's own DOCTYPE, not a host the app contacts
                host = m.group(1).rsplit("@", 1)[-1].lower()
                if not host.startswith("["):
                    host = host.split(":", 1)[0]
                host = host.strip(".")
                if host and HOST_RE.fullmatch(host):
                    out.add(host)
    return out


def read_tree(root: Path | None, policy) -> dict:
    """The facts of one tree (empty when `root` is None)."""
    facts = {"files": {}, "testers": testers_file.TestersFile(), "display_name": None, "version": None,
             "build": None, "permissions": {}, "hosts": set(), "entitlements": {}}
    if root is None:
        return facts
    ctx = checks.Context(repo=Path(root).resolve(), bundle_id=SUMMARY_BUNDLE, policy=policy)
    checks.walk_files(ctx)
    facts["files"] = {rel: hashlib.sha256(data).hexdigest() for rel, data in ctx.contents.items()}
    facts["testers"] = testers_file.parse(ctx.text(TESTERS_FILE))
    spec = checks.check_project_yml(ctx)
    declared = checks._declared_plist(ctx, spec) if spec is not None else {}
    if "CFBundleDisplayName" in declared:
        facts["display_name"] = _cap(checks._norm(declared["CFBundleDisplayName"][0]))
    for key, value, _line, _scope in (spec.settings if spec is not None else []):
        if key == "MARKETING_VERSION":
            facts["version"] = _cap(checks._norm(value), 40)
        elif key == "CURRENT_PROJECT_VERSION":
            facts["build"] = _cap(checks._norm(value), 40)
    facts["permissions"] = {str(k): _cap(v[0]) for k, v in declared.items() if USAGE_RE.fullmatch(str(k))}
    ents = {}
    if spec is not None:
        for keys, _line in spec.entitlement_props:
            for k in keys:
                ents[str(k)] = "(set in project.yml)"
        files = {rel for rel in ctx.contents if rel.lower().endswith(".entitlements")}
        files |= checks._referenced_files(ctx, spec, "entitlements", "entitlements file")
        for rel in sorted(files):
            value, err = checks._read_plist(ctx, rel)
            if err:
                continue
            for k, v in value.items():
                ents[str(k)] = _jsonable(v)
    facts["entitlements"] = ents
    hosts = set()
    for rel, data in ctx.contents.items():
        if checks._is_text(data):
            hosts |= hosts_in(checks._decoded(data))
    facts["hosts"] = hosts
    return facts


def _map_diff(before: dict, after: dict, value_key: str) -> dict:
    return {"added": [{"key": k, value_key: after[k]} for k in sorted(set(after) - set(before))],
            "removed": sorted(set(before) - set(after)),
            "changed": [{"key": k, "before": before[k], "after": after[k]}
                        for k in sorted(set(before) & set(after)) if before[k] != after[k]]}


def _pair(before, after):
    return None if before == after else {"before": before, "after": after}


def summarize(base: dict, head: dict) -> dict:
    changed = sorted(p for p in set(base["files"]) | set(head["files"])
                     if base["files"].get(p) != head["files"].get(p))
    bt, ht = base["testers"], head["testers"]
    b_friends = {p.email: p for p in bt.friends}
    h_friends = {p.email: p for p in ht.friends}

    def person(p):
        return {"email": p.email, "name": " ".join(x for x in (p.first, p.last) if x)}
    hosts_added = sorted(head["hosts"] - base["hosts"])
    return {
        "files": {"changed": len(changed), "paths": changed[:MAX_PATHS], "truncated": len(changed) > MAX_PATHS},
        "testers": {
            "friends": {"added": [person(h_friends[e]) for e in ht.friend_emails if e not in b_friends],
                        "removed": [person(b_friends[e]) for e in bt.friend_emails if e not in h_friends]},
            "external": {"added": [e for e in ht.external if e not in bt.external],
                         "removed": [e for e in bt.external if e not in ht.external]},
            "friend_role": "MARKETING",
            "bad_lines": ht.bad_lines,
        },
        "display_name": _pair(base["display_name"], head["display_name"]),
        "version": _pair(base["version"], head["version"]),
        "build": _pair(base["build"], head["build"]),
        "permissions": _map_diff(base["permissions"], head["permissions"], "text"),
        "hosts": {"added": hosts_added[:MAX_HOSTS], "removed": sorted(base["hosts"] - head["hosts"])[:MAX_HOSTS],
                  "truncated": len(hosts_added) > MAX_HOSTS},
        "entitlements": _map_diff(base["entitlements"], head["entitlements"], "value"),
        "managed": {"changed": [p for p in MANAGED_FILES if p in changed]},
    }


def changes(head_dir: Path, base_dir: Path | None, policy=None) -> dict:
    policy = policy or load_policy()
    return summarize(read_tree(base_dir, policy), read_tree(head_dir, policy))


def main(args) -> int:
    data = changes(Path(args.repo_dir), Path(args.base) if args.base else None, load_policy(args.policy))
    Path(args.json_out).write_text(json.dumps(data, sort_keys=True))
    return 0

