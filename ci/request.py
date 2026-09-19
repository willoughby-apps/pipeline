"""Request job (tags and tester lists): open the request for Andrew and, for a
tag, start the review.

    APP_TOKEN=... MONOREPO_DISPATCH=... REPO SHA KIND TAG RUN_ID RUN_URL IPA_SHA256 \\
        SOURCE_SHA256 BASE_SHA CHANGES_JSON python3 ci/request.py

1. Opens (or reuses) the issue in the guest's repo: for a tag
   "Release request: <tag>", labelled `release-request`, pinned to the SHA,
   the pipeline run and the unsigned build's SHA-256; for a tester-list change
   (KIND=testers, nothing built) "Testers request: <full sha>", labelled
   `testers-request`. The guest gets GitHub's own email for it; Andrew
   approves or rejects it from the Willoughby TestFlight page (PLAN section 5).
   Each label is created first when the repo lacks it (422 = it exists).
2. For a tag, dispatches `guest-review.yml` on ajcohen9/willoughby with the
   same pins, so the advisory review is posted on the issue.

The body @mentions ANDREW, so GitHub notifies him (he owns the org, so he can
read every guest repo), and carries the check's images from the previews ref
(`refs/willoughby/previews`, written by report.py): shown in the body and,
for the Willoughby page, in a machine-readable block

    <!-- willoughby-previews v1
    {"sha": ..., "tag": ..., "previews_commit": ...,
     "screenshot": {"url": ..., "api": ...} | null, "icon": {...} | null}
    -->

**What changed since Andrew's last approval** (PLAN section 1, 2026-09-19
decision d) is shown in the body and carried in a second block,
`willoughby-changes v1` (CHANGES_BLOCK_DOC below is its exact format). Its
facts come from the gate job's data-only reading of the approved tree and this
one (`gate/changes.py`, decrypted from the gate report by this job), re-shaped
here to known keys, types and sizes (`clean_changes`); the JSON writes `<`,
`>`, `&` and every `--` as `\\u` escapes so it cannot end the HTML comment.

Every value in the previews block is ours; the changes block carries
guest-written text (addresses, host names, permission texts) as data. Like
the pin block, both are for display: a guest can edit the body, so a reader
checks the author and edit history before trusting it.

Only an issue the pipeline's bot (BOT_LOGIN) opened is reused. A guest has write on
their repo, so they can open "Release request: v1.1" with the label before
tagging; reusing it would put our pins into an issue whose body they can edit
afterwards. The pin block is for people to read: a guest with write can edit
any issue body in their repo, even ours, so nothing may take pins from it
(PLAN sections 4 and 5: consumers read the `willoughby/release` or
`willoughby/testers-request` status our user created, plus the run).

Every value used to decide anything here is either ours or validated (repo
name, 40-hex SHA, v1.2.3 tag, numeric run id, 64-hex digests).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ghapi import APP_TOKEN_ENV, BOT_LOGIN, ORG, Client, GitHubError, validate_inputs  # noqa: E402

LABEL = "release-request"
TESTERS_LABEL = "testers-request"
LABELS = {LABEL: ("0e8a16", "A release waiting for Andrew's approval (opened by the pipeline)"),
          TESTERS_LABEL: ("1d76db", "A tester list change waiting for Andrew's approval (opened by the pipeline)")}
CHANGES_MARKER = "willoughby-changes v1"
MANAGED_FILES = (".claude/settings.json", "CLAUDE.md")
MAX_ITEMS = 100
MAX_STR = 300

CHANGES_BLOCK_DOC = """
<!-- willoughby-changes v1
{"v": 1,
 "kind": "release" | "testers",
 "repo": "willoughby-apps/<repo>",
 "sha": "<40 hex, the requested commit>",
 "tag": "v1.2" | null,
 "base_sha": "<40 hex, the last approved commit>" | null,
 "run_id": "<the check.yml run>",
 "diff_url": "https://github.com/willoughby-apps/<repo>/compare/<base>...<sha>" | ".../commit/<sha>",
 "previews": {"screenshot": {"url", "api"} | null, "icon": {"url", "api"} | null} | null,
 "review": {"requested": true | false, "marker": "<!-- willoughby-review sha=<sha> -->"},
 "summary": "ok" | "unavailable",
 "files": {"changed": n, "paths": [str], "truncated": bool} | null,
 "testers": {"app": {"repo", "bundle_id"}, "friend_role": "MARKETING",
             "friends": {"added": [{"email", "name"}], "removed": [{"email", "name"}]},
             "external": {"added": [email], "removed": [email]}, "bad_lines": n} | null,
 "display_name": {"before": str | null, "after": str | null} | null,
 "version": {"before", "after"} | null,
 "build": {"before", "after"} | null,
 "permissions": {"added": [{"key", "text"}], "removed": [key], "changed": [{"key", "before", "after"}]} | null,
 "hosts": {"added": [host], "removed": [host], "truncated": bool} | null,
 "entitlements": {"added": [{"key", "value"}], "removed": [key], "changed": [{"key", "before", "after"}]} | null,
 "managed": {"changed": [".claude/settings.json" | "CLAUDE.md"]} | null}
-->
A null section means "no change" (display_name, version, build) or, with
summary "unavailable", "not known". Lists hold at most 100 items and strings
300 characters.
"""
MONOREPO = "ajcohen9/willoughby"
REVIEW_WORKFLOW = "guest-review.yml"
ANDREW = "ajcohen9"
PREVIEWS_MARKER = "willoughby-previews v1"


def previews(repo: str, sha: str, commit: str, screenshot: str, icon: str) -> dict | None:
    """The previews block's data, or None when the report stored nothing. The
    paths must be exactly report.py's names for this tag check."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        return None
    want = {"screenshot": f"previews/{sha}-tag.png", "icon": f"previews/{sha}-tag-icon.png"}
    out = {"previews_commit": commit, "screenshot": None, "icon": None}
    for name, path in (("screenshot", screenshot), ("icon", icon)):
        if path and path == want[name]:
            out[name] = {"url": f"https://github.com/{ORG}/{repo}/blob/{commit}/{path}?raw=true",
                         "api": f"repos/{ORG}/{repo}/contents/{path}?ref={commit}"}
    return out if (out["screenshot"] or out["icon"]) else None


def issue_title(tag: str) -> str:
    return f"Release request: {tag}"


def testers_title(sha: str) -> str:
    return f"Testers request: {sha}"


# ------------------------------------------------------------ the changes


def _s(v, limit=MAX_STR):
    return v[:limit] if isinstance(v, str) else None


def _strs(v):
    return [x[:MAX_STR] for x in (v if isinstance(v, list) else []) if isinstance(x, str)][:MAX_ITEMS]


def _pair(v):
    if not isinstance(v, dict):
        return None
    return {"before": _s(v.get("before")), "after": _s(v.get("after"))}


def _value(v, depth=0):
    """An entitlement value (JSON from a plist), bounded."""
    if depth > 3:
        return None
    if isinstance(v, dict):
        return {str(k)[:100]: _value(x, depth + 1) for k, x in list(v.items())[:MAX_ITEMS]}
    if isinstance(v, list):
        return [_value(x, depth + 1) for x in v[:MAX_ITEMS]]
    if isinstance(v, (bool, int, float)) or v is None:
        return v
    return str(v)[:MAX_STR]


def _map(v, value_key):
    if not isinstance(v, dict):
        return None
    added = [{"key": _s(i.get("key")) or "", value_key: _value(i.get(value_key))}
             for i in (v.get("added") or [])[:MAX_ITEMS] if isinstance(i, dict)]
    changed = [{"key": _s(i.get("key")) or "", "before": _value(i.get("before")), "after": _value(i.get("after"))}
               for i in (v.get("changed") or [])[:MAX_ITEMS] if isinstance(i, dict)]
    return {"added": added, "removed": _strs(v.get("removed")), "changed": changed}


def _people(v):
    return [{"email": _s(i.get("email")) or "", "name": _s(i.get("name"), 60) or ""}
            for i in (v if isinstance(v, list) else [])[:MAX_ITEMS] if isinstance(i, dict)]


def clean_changes(raw) -> dict | None:
    """gate/changes.py's output, reduced to the known keys, types and sizes."""
    if not isinstance(raw, dict):
        return None
    files = raw.get("files") if isinstance(raw.get("files"), dict) else {}
    testers = raw.get("testers") if isinstance(raw.get("testers"), dict) else {}
    friends = testers.get("friends") if isinstance(testers.get("friends"), dict) else {}
    external = testers.get("external") if isinstance(testers.get("external"), dict) else {}
    hosts = raw.get("hosts") if isinstance(raw.get("hosts"), dict) else {}
    managed = raw.get("managed") if isinstance(raw.get("managed"), dict) else {}
    changed = files.get("changed")
    bad = testers.get("bad_lines")
    return {
        "files": {"changed": changed if isinstance(changed, int) and changed >= 0 else 0,
                  "paths": _strs(files.get("paths")), "truncated": files.get("truncated") is True},
        "testers": {"friend_role": "MARKETING",
                    "friends": {"added": _people(friends.get("added")), "removed": _people(friends.get("removed"))},
                    "external": {"added": _strs(external.get("added")), "removed": _strs(external.get("removed"))},
                    "bad_lines": bad if isinstance(bad, int) and bad >= 0 else 0},
        "display_name": _pair(raw.get("display_name")),
        "version": _pair(raw.get("version")),
        "build": _pair(raw.get("build")),
        "permissions": _map(raw.get("permissions"), "text"),
        "hosts": {"added": _strs(hosts.get("added")), "removed": _strs(hosts.get("removed")),
                  "truncated": hosts.get("truncated") is True},
        "entitlements": _map(raw.get("entitlements"), "value"),
        "managed": {"changed": [p for p in _strs(managed.get("changed")) if p in MANAGED_FILES]},
    }


def load_changes(path: str) -> dict | None:
    try:
        return clean_changes(json.loads(Path(path).read_text()))
    except (OSError, ValueError):
        return None


def changes_block(repo: str, sha: str, kind: str, tag: str | None, base: str, run_id: str, bundle_id: str,
                  pv: dict | None, changes: dict | None) -> dict:
    empty = dict.fromkeys(("files", "testers", "display_name", "version", "build", "permissions", "hosts",
                           "entitlements", "managed"))
    data = {"v": 1, "kind": "release" if kind == "tag" else "testers", "repo": f"{ORG}/{repo}", "sha": sha,
            "tag": tag if kind == "tag" else None, "base_sha": base or None, "run_id": run_id,
            "diff_url": (f"https://github.com/{ORG}/{repo}/compare/{base}...{sha}" if base
                         else f"https://github.com/{ORG}/{repo}/commit/{sha}"),
            "previews": {"screenshot": pv["screenshot"], "icon": pv["icon"]} if pv else None,
            "review": {"requested": kind == "tag", "marker": f"<!-- willoughby-review sha={sha} -->"},
            "summary": "ok" if changes is not None else "unavailable", **empty}
    if changes is not None:
        data.update(changes)
        data["testers"] = {"app": {"repo": repo, "bundle_id": bundle_id}, **changes["testers"]}
    return data


def encode_data(data: dict) -> str:
    """JSON that cannot close the HTML comment or open a tag (review/release.py's rule)."""
    text = json.dumps(data, sort_keys=True, ensure_ascii=False)
    for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
        text = text.replace(ch, esc)
    return text.replace("--", "-\\u002d")


def parse_changes_block(body: str) -> dict | None:
    m = re.search(r"<!-- " + re.escape(CHANGES_MARKER) + r"\n(.*?)\n-->", body or "", re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except ValueError:
        return None


def _fence_line(v) -> str:
    s = str(v if v is not None else "")
    return s.replace("\r", " ").replace("\n", " ").replace("`", "'")[:MAX_STR]


def changes_lines(data: dict) -> list[str]:
    """The visible summary. Guest text only inside a text fence, one line each."""
    out = [f"**What changed since Andrew's last approval**: [see the code changes]({data['diff_url']})", ""]
    if data["summary"] != "ok":
        return out + ["(The summary could not be computed; look at the code changes.)", ""]
    lines = []
    t = data["testers"]
    for p in t["friends"]["added"]:
        lines.append(f"tester added: {p['email']} {p['name']} (Andrew's team, Marketing role, this app only)")
    for p in t["friends"]["removed"]:
        lines.append(f"tester removed: {p['email']}")
    for e in t["external"]["added"]:
        lines.append(f"outside tester added: {e} (external group)")
    for e in t["external"]["removed"]:
        lines.append(f"outside tester removed: {e}")
    for key, word in (("display_name", "name under the icon"), ("version", "version"), ("build", "build number")):
        if data[key]:
            lines.append(f"{word}: {data[key]['before']} -> {data[key]['after']}")
    for p in (data["permissions"] or {}).get("added", []):
        lines.append(f"new permission: {p['key']}: {p['text']}")
    for p in (data["permissions"] or {}).get("changed", []):
        lines.append(f"permission text changed: {p['key']}: {p['after']}")
    for k in (data["permissions"] or {}).get("removed", []):
        lines.append(f"permission removed: {k}")
    for h in data["hosts"]["added"]:
        lines.append(f"new host contacted: {h}")
    for e in (data["entitlements"] or {}).get("added", []):
        lines.append(f"new entitlement: {e['key']}")
    for e in (data["entitlements"] or {}).get("changed", []):
        lines.append(f"entitlement changed: {e['key']}")
    for k in (data["entitlements"] or {}).get("removed", []):
        lines.append(f"entitlement removed: {k}")
    for m in data["managed"]["changed"]:
        lines.append(f"Andrew's managed file changed: {m}")
    lines.append(f"files changed: {data['files']['changed']}")
    return out + ["```text", *[_fence_line(x) for x in lines], "```", ""]


def ensure_label(client: Client, repo: str, name: str) -> None:
    try:
        have = {lb.get("name") for lb in client.get(f"/repos/{ORG}/{repo}/labels?per_page=100") or []}
    except GitHubError:
        have = set()
    if name in have:
        return
    color, desc = LABELS[name]
    try:
        client.post(f"/repos/{ORG}/{repo}/labels", {"name": name, "color": color, "description": desc})
    except GitHubError as e:
        if e.status != 422:  # already_exists
            raise


def previews_lines(sha: str, tag: str, pv: dict | None) -> list[str]:
    if not pv:
        return []
    lines = []
    shot, icon = pv["screenshot"], pv["icon"]
    if shot and icon:
        lines += ["| App icon | Screenshot |", "|:---:|:---:|",
                  f'| <img src="{icon["url"]}" width="120" alt="app icon"> '
                  f'| <img src="{shot["url"]}" width="300" alt="screenshot"> |', ""]
    elif shot:
        lines += [f'<img src="{shot["url"]}" width="300" alt="screenshot">', ""]
    else:
        lines += [f'<img src="{icon["url"]}" width="120" alt="app icon">', ""]
    data = {"sha": sha, "tag": tag, **pv}
    lines += [f"<!-- {PREVIEWS_MARKER}", json.dumps(data, sort_keys=True), "-->"]
    return lines


def issue_body(repo: str, sha: str, tag: str, run_id: str, run_url: str, ipa_sha256: str,
               source_sha256: str, pv: dict | None = None, changes: dict | None = None) -> str:
    return "\n".join([
        f"Version **{tag}** passed the safety checks, compiles and opens in the simulator.",
        "",
        f"@{ANDREW}: this is waiting for your approval in Willoughby.",
        "",
        "Andrew approves or rejects it. When he approves, it goes to TestFlight and this issue closes.",
        "",
        *previews_lines(sha, tag, pv),
        *(changes_lines(changes) + [f"<!-- {CHANGES_MARKER}", encode_data(changes), "-->"] if changes else []),
        "<!-- willoughby-release",
        f"repo: {ORG}/{repo}",
        f"sha: {sha}",
        f"tag: {tag}",
        f"pipeline_run: {run_id}",
        f"unsigned_sha256: {ipa_sha256}",
        f"source_sha256: {source_sha256}",
        "-->",
        f"Commit: {sha}",
        f"Pipeline run: {run_url}",
    ])


def testers_body(repo: str, sha: str, run_id: str, run_url: str, source_sha256: str, changes: dict) -> str:
    return "\n".join([
        "Only the tester list (`testers.txt`) changed since Andrew's last approval, so nothing was built.",
        "",
        f"@{ANDREW}: this tester change is waiting for your approval in Willoughby.",
        "",
        "When Andrew approves, the people added are invited (Apple emails them) and the people removed "
        "lose access. Until then nothing changes.",
        "",
        *changes_lines(changes),
        f"<!-- {CHANGES_MARKER}", encode_data(changes), "-->",
        "<!-- willoughby-testers",
        f"repo: {ORG}/{repo}",
        f"sha: {sha}",
        f"pipeline_run: {run_id}",
        f"source_sha256: {source_sha256}",
        "-->",
        f"Commit: {sha}",
        f"Pipeline run: {run_url}",
    ])


def find_open_request(client: Client, repo: str, tag: str, login: str, label: str = LABEL,
                      title: str | None = None) -> dict | None:
    """Our own open request issue for `tag` (or `title`). An issue anyone else
    opened (the guest can, with the same title and label) is never reused."""
    want = title or issue_title(tag)
    for issue in client.paginate(f"/repos/{ORG}/{repo}/issues?state=open&labels={label}", limit=300):
        if (issue.get("title") == want and "pull_request" not in issue
                and (issue.get("user") or {}).get("login") == login):
            return issue
    return None


def main() -> int:
    repo, sha, tag = os.environ.get("REPO", ""), os.environ.get("SHA", ""), os.environ.get("TAG", "")
    kind = os.environ.get("KIND", "") or "tag"
    run_id, run_url = os.environ.get("RUN_ID", ""), os.environ.get("RUN_URL", "")
    ipa, source = os.environ.get("IPA_SHA256", ""), os.environ.get("SOURCE_SHA256", "")
    base = os.environ.get("BASE_SHA", "")
    validate_inputs(repo, sha, kind, tag)
    if kind not in ("tag", "testers"):
        raise SystemExit("a request is for a tag or a tester list")
    if not re.fullmatch(r"[0-9]{1,20}", run_id) or not re.fullmatch(r"[0-9a-f]{64}", source) \
            or (kind == "tag" and not re.fullmatch(r"[0-9a-f]{64}", ipa)):
        raise SystemExit("run id, unsigned build digest or source digest missing")
    if base and not re.fullmatch(r"[0-9a-f]{40}", base):
        raise SystemExit("the approved base is not a commit SHA")
    pv = previews(repo, sha, os.environ.get("PREVIEWS_COMMIT", ""), os.environ.get("PREVIEW_SCREENSHOT", ""),
                  os.environ.get("PREVIEW_ICON", "")) if kind == "tag" else None
    changes = load_changes(os.environ.get("CHANGES_JSON", "")) if os.environ.get("CHANGES_JSON") else None
    data = changes_block(repo, sha, kind, tag or None, base, run_id, os.environ.get("BUNDLE_ID", ""), pv, changes)
    org = Client.from_env(APP_TOKEN_ENV)
    login = BOT_LOGIN  # an installation token cannot call GET /user
    if kind == "testers":
        label, title = TESTERS_LABEL, testers_title(sha)
        body = testers_body(repo, sha, run_id, run_url, source, data)
    else:
        label, title = LABEL, issue_title(tag)
        body = issue_body(repo, sha, tag, run_id, run_url, ipa, source, pv, data)
    ensure_label(org, repo, label)
    existing = find_open_request(org, repo, tag, login, label, title)
    if existing:
        # Same commit re-checked (say, after an expired artifact): re-pin the same issue.
        org.json("PATCH", f"/repos/{ORG}/{repo}/issues/{existing['number']}", {"body": body})
        number = existing["number"]
    else:
        number = org.post(f"/repos/{ORG}/{repo}/issues", {"title": title, "body": body, "labels": [label]})["number"]
    print(f"{'testers' if kind == 'testers' else 'release'} request is issue #{number}")
    if kind == "testers":
        return 0
    mono = Client.from_env("MONOREPO_DISPATCH")
    try:
        mono.post(f"/repos/{MONOREPO}/actions/workflows/{REVIEW_WORKFLOW}/dispatches", {
            "ref": "main",
            "inputs": {"repo": f"{ORG}/{repo}", "sha": sha, "tag": tag, "issue": str(number), "run_id": run_id,
                       "source_sha256": source}})
    except GitHubError as e:
        raise SystemExit(f"could not start {REVIEW_WORKFLOW} on {MONOREPO}: HTTP {e.status}") from None
    print(f"dispatched {REVIEW_WORKFLOW}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
