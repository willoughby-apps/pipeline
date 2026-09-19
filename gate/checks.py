"""The deterministic hard gate: static checks over a guest repo, read as data.

Nothing in here executes anything from the repo. Files are listed without
following symlinks, read as bytes, and parsed with safe parsers (yamlsafe,
json, plistlib). No xcodegen, no swift, no package resolution, no git.
"""
from __future__ import annotations

import json
import os
import plistlib
import posixpath
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import yamlsafe
from .policy import Policy


@dataclass
class Failure:
    rule: str
    file: str | None
    line: int | None
    detail: str


@dataclass
class Context:
    repo: Path
    bundle_id: str
    policy: Policy
    disabled_rules: frozenset = frozenset()
    failures: list = field(default_factory=list)
    # repo-relative posix path -> bytes, for every regular file under the caps
    contents: dict = field(default_factory=dict)
    # The main app icon that passed app_icon.invalid (repo-relative), for the report.
    app_icon: str | None = None

    def fail(self, rule: str, file: str | None, line: int | None, detail: str):
        if rule not in self.policy.rules:
            raise KeyError(f"gate raised rule {rule!r}, which policy.yml does not define")
        if rule in self.disabled_rules:
            return
        key = (rule, file, line, detail)
        if any((f.rule, f.file, f.line, f.detail) == key for f in self.failures):
            return
        self.failures.append(Failure(rule, file, line, detail))

    def text(self, rel: str) -> str | None:
        data = self.contents.get(rel)
        return None if data is None else data.decode("utf-8", errors="replace")

    def find(self, path) -> str | None:
        """The loaded file that `path` names on the build machine's volume, or None.

        APFS on the Mini is case- and normalization-insensitive, so `Project.yml`,
        `./HelloApp//X.plist` and `helloapp/x.PLIST` all open the same file as the
        spelling a policy or a build setting uses. Every lookup of a path from the
        policy or from project.yml goes through here, never through `contents`.
        """
        if not isinstance(path, str):
            return None
        key = fold_path(path)
        for rel in self.contents:
            if fold_path(rel) == key:
                return rel
        return None


def fold_path(path: str) -> str:
    """A path as the case-insensitive volume compares it: normalized, no `./`,
    no doubled slashes, casefolded."""
    p = posixpath.normpath(path.strip().replace("\\", "/"))
    while p.startswith("./"):
        p = p[2:]
    return unicodedata.normalize("NFC", p).casefold()


def fold_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# ---------------------------------------------------------------- file walk


def walk_files(ctx: Context):
    """Inventory the checkout. Records symlinks, oversized files, forbidden
    names and suffixes; loads every other regular file's bytes."""
    pol = ctx.policy["files"]
    xcode_suffixes = tuple(s.lower() for s in pol["forbidden_xcode_suffixes"])
    binary_suffixes = tuple(s.lower() for s in pol["binary_extensions"])
    # Compared casefolded: `.GitleaksIgnore` is `.gitleaksignore` to the scanner
    # that opens it on a case-insensitive volume.
    forbidden_names = {fold_name(k): v for k, v in pol["forbidden_names"].items()}
    allowed_xcconfig = {fold_path(p) for p in pol["allowed_xcconfig_paths"]}
    max_file = int(pol["max_file_bytes"])
    allowed_ext = set(_allowed_extensions(pol))
    allowed_names = {fold_name(n) for n in pol["text_names"]}
    total = 0
    count = 0
    root = ctx.repo
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = PurePosixPath(Path(dirpath).relative_to(root).as_posix())
        keep = []
        for d in sorted(dirnames):
            rel = d if str(rel_dir) == "." else f"{rel_dir.as_posix()}/{d}"
            full = Path(dirpath) / d
            if d == ".git" and str(rel_dir) == ".":
                continue
            if full.is_symlink():
                ctx.fail("files.symlink", rel, None, f"{rel} is a symbolic link to {os.readlink(full)}.")
                continue
            low = d.lower()
            if low.endswith(xcode_suffixes):
                ctx.fail("files.xcode_project", rel, None, f"{rel} is a committed Xcode project file.")
                continue
            if low.endswith(binary_suffixes):
                ctx.fail("files.binary", rel, None, f"{rel} is a compiled bundle or library.")
                continue
            if fold_name(d) in forbidden_names:
                ctx.fail("files.forbidden_name", rel, None, f"{rel} {forbidden_names[fold_name(d)]}.")
                continue
            keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = name if str(rel_dir) == "." else f"{rel_dir.as_posix()}/{name}"
            full = Path(dirpath) / name
            if name == ".git" and str(rel_dir) == ".":
                continue
            st = full.lstat()
            if full.is_symlink():
                ctx.fail("files.symlink", rel, None, f"{rel} is a symbolic link to {os.readlink(full)}.")
                continue
            if not full.is_file():
                ctx.fail("files.binary", rel, None, f"{rel} is not a regular file.")
                continue
            count += 1
            total += st.st_size
            low = name.lower()
            flagged = False
            if fold_name(name) in forbidden_names:
                flagged = True
                ctx.fail("files.forbidden_name", rel, None, f"{rel} {forbidden_names[fold_name(name)]}.")
            if low.endswith(xcode_suffixes):
                flagged = True
                ctx.fail("files.xcode_project", rel, None, f"{rel} is a committed Xcode project file.")
            if low.endswith(".xcconfig") and fold_path(rel) not in allowed_xcconfig:
                flagged = True
                ctx.fail("files.xcconfig", rel, None, f"{rel} is an .xcconfig file, which is not on the allowlist.")
            if low.endswith(binary_suffixes):
                flagged = True
                ctx.fail("files.binary", rel, None, f"{rel} has a binary or archive file extension.")
            # A file already refused above is not refused twice. Everything else
            # must be a kind the policy names: XcodeGen compiles any .c, .m, .s
            # or .metal it finds under a sources path, whatever the gate's
            # Swift-only rules say.
            if not flagged and fold_name(name) not in allowed_names and _suffix(name) not in allowed_ext:
                ctx.fail("files.type_not_allowed", rel, None,
                         f"{rel} is not Swift, an image or a text file the policy allows.")
            if st.st_size > max_file:
                ctx.fail("files.size", rel, None,
                         f"{rel} is {st.st_size:,} bytes; the limit is {max_file:,} bytes.")
                continue
            with open(full, "rb") as fh:
                ctx.contents[rel] = fh.read(max_file + 1)
    if total > int(pol["max_repo_bytes"]):
        ctx.fail("files.size", None, None,
                 f"The repo is {total:,} bytes; the limit is {int(pol['max_repo_bytes']):,} bytes.")
    if count > int(pol["max_file_count"]):
        ctx.fail("files.size", None, None,
                 f"The repo has {count:,} files; the limit is {int(pol['max_file_count']):,}.")


def _suffix(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def _allowed_extensions(pol) -> list[str]:
    return [e.lower() for e in pol["text_extensions"]] + [e.lower() for e in pol["image_types"]]


_CONTROL = re.compile("[\x00-\x08\x0e-\x1f\x7f]")


def _is_text(data: bytes) -> bool:
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return _CONTROL.search(text) is None


def _zip_members(data: bytes) -> list[str] | None:
    """The member names if a zip reader opens `data` (from its end, as every
    zip reader does), else None."""
    import io
    import zipfile
    if b"PK\x05\x06" not in data[-(65536 + 22):]:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return zf.namelist()
    except Exception:  # zipfile raises several unrelated types on non-zip data
        return None


def _binary_kind(ctx: Context, rel: str, data: bytes) -> str | None:
    """Why `data` is not the kind of file its name says, or None."""
    pol = ctx.policy["files"]
    for label, h in pol["binary_magic"].items():
        if data.startswith(bytes.fromhex(h)):
            return f"is a {label}, whatever its name says"
    for label, at in pol.get("binary_magic_at", {}).items():
        off = int(at["offset"])
        magic = bytes.fromhex(at["hex"])
        if data[off:off + len(magic)] == magic:
            return f"is a {label}, whatever its name says"
    members = _zip_members(data)
    if members is not None:
        shown = ", ".join(members[:5]) + (" ..." if len(members) > 5 else "")
        return f"opens as a zip archive ({shown or 'empty'})"
    name = rel.rsplit("/", 1)[-1]
    ext = _suffix(name)
    if ext in {e.lower() for e in pol["image_types"]}:
        spec = {k.lower(): v for k, v in pol["image_types"].items()}[ext]
        off = int(spec.get("offset", 0))
        if not any(data[off:off + len(bytes.fromhex(m))] == bytes.fromhex(m) for m in spec["magic"]):
            return f"is not really a {ext[1:].upper()} image"
        trailer = spec.get("trailer")
        if trailer and not data.endswith(bytes.fromhex(trailer)):
            return f"has data after the end of its {ext[1:].upper()} image"
    elif ext in {e.lower() for e in pol["text_extensions"]} or fold_name(name) in {fold_name(n) for n in pol["text_names"]}:
        if not _is_text(data):
            return "is not plain text, whatever its name says"
    return None


def _gitattributes_set(text: str, forbidden) -> list[tuple[int, str]]:
    """(line, attribute) for each forbidden attribute a .gitattributes line sets.

    Parsed the way git's attr.c reads a line: blanks (space, tab, CR, LF) skipped,
    a line whose first non-blank is `#` is a comment, then a pattern (or an
    `[attr]name` macro definition) followed by attributes. `-attr` unsets and
    `!attr` unspecifies, which change nothing; `attr` or `attr=value` set it.
    Tokens are split on any whitespace, a superset of git's blanks, so the gate
    can only see more attributes than git does, never fewer. `export-subst` and
    `export-ignore` matter here because the pipeline builds from GitHub's
    tarball, which is `git archive` output: one rewrites a file's text, the
    other drops a file, so what is built would differ from the tree people and
    the review read.
    """
    names = {str(n).rstrip("=").casefold() for n in forbidden}
    found = []
    for i, line in enumerate(text.split("\n"), 1):
        body = line.strip(" \t\r\n")
        if not body or body.startswith("#"):
            continue
        for tok in body.split()[1:]:
            if tok.startswith(("-", "!")):
                continue
            attr = tok.split("=", 1)[0]
            if attr.casefold() in names:
                found.append((i, attr))
    return found


def check_contents(ctx: Context):
    pol = ctx.policy["files"]
    markers = pol["forbidden_text_markers"]
    secret_res = [(label, re.compile(p)) for label, p in ctx.policy["secrets"]["patterns"].items()]
    swift_res = [(re.compile(p["pattern"], re.MULTILINE), p["what"])
                 for p in ctx.policy["swift"]["forbidden_patterns"]]
    risky_res = [(re.compile(p["pattern"], re.MULTILINE), p["what"])
                 for p in ctx.policy["swift"]["risky_api_patterns"]]
    for rel, data in sorted(ctx.contents.items()):
        why = _binary_kind(ctx, rel, data)
        if why:
            ctx.fail("files.binary", rel, None, f"{rel} {why}.")
        text = data.decode("utf-8", errors="replace")
        for marker, why in markers.items():
            idx = text.find(marker)
            if idx >= 0:
                ctx.fail("files.scanner_suppression", rel, _line_of(text, idx),
                         f"{rel} contains \"{marker}\", which {why}.")
        secret_lines = set()
        for label, rx in secret_res:
            for m in rx.finditer(text):
                line = _line_of(text, m.start())
                if line not in secret_lines:  # one report per line, whichever shape matched first
                    secret_lines.add(line)
                    ctx.fail("secrets.hardcoded", rel, line, f"{rel} line {line} contains what looks like a {label}.")
        name = rel.rsplit("/", 1)[-1]
        if fold_name(name) == ".gitattributes":
            for i, attr in _gitattributes_set(text, pol["gitattributes_forbidden"]):
                ctx.fail("files.gitattributes", rel, i, f"{rel} line {i} sets `{attr}`.")
        if rel.lower().endswith(".swift"):
            for rx, what in swift_res:
                m = rx.search(text)
                if m:
                    ctx.fail("swift.plugin_or_macro", rel, _line_of(text, m.start()), f"{rel} {what}.")
            for rx, what in risky_res:
                m = rx.search(text)
                if m:
                    ctx.fail("swift.risky_api", rel, _line_of(text, m.start()), f"{rel} {what}.")


# ---------------------------------------------------------- infrastructure


def _decoded(data: bytes) -> str:
    """Text as an editor would show it: UTF-16 when it carries a byte-order
    mark (a UTF-8 read of it puts a NUL between every letter, so a host
    written in UTF-16 would never match), else UTF-8 with replacement."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


# The escapes a compiler, a parser or a URL loader turns back into the same
# characters: Swift \u{2E}; JSON/YAML/.strings \u002E and \U002E; YAML and C
# \U0000002E and \x2E; XML/HTML &#46; &#x2E; (and a few named entities); URL %2E.
_ESCAPE = re.compile(
    r"\\u\{(?P<swift>[0-9a-fA-F]{1,8})\}"
    r"|\\U(?P<u8>[0-9a-fA-F]{8})"
    r"|\\[uU](?P<u4>[0-9a-fA-F]{4})"
    r"|\\x(?P<x2>[0-9a-fA-F]{2})"
    r"|&#[xX](?P<xmlhex>[0-9a-fA-F]{1,8});?"
    r"|&#(?P<xmldec>[0-9]{1,10});?"
    r"|&(?P<named>amp|period|colon|sol|quot|apos|lt|gt|commat|num|lsqb|rsqb|lbrack|rbrack);"
    r"|%(?P<pct>[0-9a-fA-F]{2})")
_NAMED = {"amp": "&", "period": ".", "colon": ":", "sol": "/", "quot": '"', "apos": "'", "lt": "<", "gt": ">",
          "commat": "@", "num": "#", "lsqb": "[", "rsqb": "]", "lbrack": "[", "rbrack": "]"}
# Full stops a URL parser (UTS 46) maps to "." after NFKC has folded the
# fullwidth forms.
_DOTS = str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."})


def _unescape_once(text: str) -> str:
    def repl(m):
        if m.group("named"):
            return _NAMED[m.group("named")]
        code = next(v for k, v in m.groupdict().items() if v is not None and k != "named")
        n = int(code, 10 if m.group("xmldec") else 16)
        if m.group("u8") and not 0 < n <= 0x10FFFF:
            # Not an 8-digit code point, so the .strings 4-digit form: \U003A then text.
            n, rest = int(code[:4], 16), code[4:]
            return chr(n) + rest if n else m.group(0)
        return chr(n) if 0 < n <= 0x10FFFF else m.group(0)
    return _ESCAPE.sub(repl, text)


def host_forms(line: str) -> list[str]:
    """The line as written, and as it reads once escapes are decoded (up to
    three layers, for `&amp;#46;`) and Unicode is folded the way a URL parser
    folds a host name."""
    decoded = line
    for _ in range(3):
        nxt = _unescape_once(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    folded = unicodedata.normalize("NFKC", decoded).translate(_DOTS)
    return [line] if folded == line else [line, folded]


def check_infrastructure(ctx: Context):
    """No file may name Andrew's own servers or networks: the app brings its own.

    Matched line by line, on the text as written and as decoded (host_forms),
    so an address spelled with escapes a build or the app turns back into
    characters is still the address."""
    hosts = [(re.compile(h["pattern"], re.IGNORECASE), h["what"])
             for h in ctx.policy["infrastructure"]["forbidden_hosts"]]
    for rel, data in sorted(ctx.contents.items()):
        text = _decoded(data)
        for line_no, line in enumerate(text.split("\n"), 1):
            for form in host_forms(line):
                for rx, what in hosts:
                    for m in rx.finditer(form):
                        ctx.fail("infra.andrews_servers", rel, line_no,
                                 f"{rel} line {line_no} names {m.group(0)!r}: {what}.")


# ------------------------------------------------------------- project.yml


def _norm(value) -> str:
    if isinstance(value, bool):
        return "YES" if value else "NO"
    return str(value).strip()


def _safe_relpath(value) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    v = value.strip()
    if v.startswith(("/", "~", "\\")) or "$" in v or ":" in v.split("/")[0]:
        return False
    return ".." not in PurePosixPath(v.replace("\\", "/")).parts


class ProjectSpec:
    """What the project.yml checks learned, for the later checks."""

    def __init__(self):
        self.settings = []  # (key, value, line, scope) scope: "project" or target name
        self.app_target: str | None = None
        self.target_types: dict = {}
        self.info_properties: dict = {}
        self.entitlement_props: list = []  # (keys, line)
        self.referenced_paths: dict = {}  # purpose -> [(path, line)]
        # Folded paths XcodeGen writes itself from info/entitlements `path`, so
        # a setting may name them before they exist.
        self.generated_paths: set = set()
        self.packages: dict = {}  # name -> (url, version, line)


def check_project_yml(ctx: Context) -> ProjectSpec | None:
    pol = ctx.policy["project_yml"]
    rel = pol["path"]
    spec = ProjectSpec()
    # XcodeGen opens `project.yml` on a case-insensitive volume, so `Project.yml`
    # is what it reads. The gate insists on the exact spelling as a regular file
    # it loaded, and fails on any other spelling beside or instead of it.
    variants = sorted(n for n in os.listdir(ctx.repo) if fold_name(n) == fold_name(rel) and n != rel)
    for name in variants:
        ctx.fail("project_yml.missing", name, None,
                 f"{name} is spelled differently from {rel}; the build machine would read it as {rel}, "
                 f"so it must be named exactly {rel}.")
    data = ctx.contents.get(rel)
    if data is None:
        if os.path.lexists(ctx.repo / rel) and not variants:
            ctx.fail("project_yml.missing", rel, None, f"{rel} is not a regular file the checker could read.")
        elif not variants:
            ctx.fail("project_yml.missing", rel, None, f"{rel} does not exist.")
        return None
    if len(data) > int(pol["max_bytes"]):
        ctx.fail("project_yml.invalid", rel, None, f"{rel} is larger than {int(pol['max_bytes']):,} bytes.")
        return None
    try:
        doc = yamlsafe.load(data.decode("utf-8"))
    except UnicodeDecodeError:
        ctx.fail("project_yml.invalid", rel, None, f"{rel} is not UTF-8 text.")
        return None
    except yamlsafe.UnsafeYAML as exc:
        ctx.fail("project_yml.invalid", rel, exc.line, f"{rel}: {exc}.")
        return None
    if not isinstance(doc, dict):
        ctx.fail("project_yml.invalid", rel, None, f"{rel} is not a mapping of settings.")
        return None

    _forbidden_keys(ctx, rel, doc, pol["forbidden_keys"])

    for key in doc:
        if key not in pol["allowed_top_level_keys"] and key not in pol["forbidden_keys"]:
            ctx.fail("project_yml.unknown_key", rel, doc.line_of(key), f"{rel} has top-level key {key!r}.")

    app_cfg = ctx.policy["app"]
    want_dt = str(app_cfg["deployment_target"])
    options = doc.get("options") or {}
    options_dt = None
    if not isinstance(options, dict):
        ctx.fail("project_yml.invalid", rel, doc.line_of("options"), f"{rel}: options is not a mapping.")
        options = {}
    for key in options:
        if key not in pol["allowed_option_keys"] and key not in pol["forbidden_keys"]:
            ctx.fail("project_yml.unknown_key", rel, options.line_of(key), f"{rel} has option {key!r}.")
    if "deploymentTarget" in options:
        dt = options["deploymentTarget"]
        line = options.line_of("deploymentTarget")
        if not isinstance(dt, dict) or set(dt) != {app_cfg["platform"]}:
            ctx.fail("project_yml.platform", rel, line,
                     f"{rel}: options.deploymentTarget must name {app_cfg['platform']} only.")
        else:
            options_dt = _norm(dt[app_cfg["platform"]])
            if options_dt != want_dt:
                ctx.fail("project_yml.deployment_target", rel, line,
                         f"{rel}: options.deploymentTarget.{app_cfg['platform']} is {options_dt!r}, not {want_dt!r}.")

    if "settings" in doc:
        _collect_settings(ctx, rel, doc["settings"], doc.line_of("settings"), "project", spec)

    packages = doc.get("packages") or {}
    if not isinstance(packages, dict):
        ctx.fail("project_yml.invalid", rel, doc.line_of("packages"), f"{rel}: packages is not a mapping.")
        packages = {}
    _check_packages(ctx, rel, packages, spec)

    targets = doc.get("targets")
    if not isinstance(targets, dict) or not targets:
        ctx.fail("project_yml.target_type", rel, doc.line_of("targets"), f"{rel} defines no targets.")
        targets = {}
    for name, target in targets.items():
        tline = targets.line_of(name)
        if not isinstance(target, dict):
            ctx.fail("project_yml.invalid", rel, tline, f"{rel}: target {name!r} is not a mapping.")
            continue
        _check_target(ctx, rel, name, target, tline, packages, targets, spec, options_dt)

    counts = {}
    for name, t in spec.target_types.items():
        counts[t] = counts.get(t, 0) + 1
    for ttype, limits in app_cfg["target_types"].items():
        n = counts.get(ttype, 0)
        if n < limits["min"] or n > limits["max"]:
            ctx.fail("project_yml.target_type", rel, doc.line_of("targets"),
                     f"{rel} has {n} {ttype} target(s); allowed {limits['min']} to {limits['max']}.")

    _check_settings(ctx, rel, spec)
    return spec


def _forbidden_keys(ctx, rel, node, forbidden, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if key in forbidden:
                ctx.fail("project_yml.forbidden_key", rel, node.line_of(key),
                         f"{rel} sets {here!r}, which {forbidden[key]}.")
            _forbidden_keys(ctx, rel, value, forbidden, here)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _forbidden_keys(ctx, rel, item, forbidden, f"{path}[{i}]")


def _collect_settings(ctx, rel, settings, line, scope, spec):
    if not isinstance(settings, dict):
        ctx.fail("project_yml.invalid", rel, line, f"{rel}: settings for {scope} is not a mapping.")
        return
    structured = {"base", "configs", "groups"}
    if set(settings) & structured:
        for key in settings:
            if key not in structured:
                ctx.fail("project_yml.build_setting", rel, settings.line_of(key),
                         f"{rel}: {scope} settings mixes {key!r} with base/configs.")
        if "base" in settings:
            _flat_settings(ctx, rel, settings["base"], settings.line_of("base"), scope, spec)
        configs = settings.get("configs")
        if configs is not None:
            if not isinstance(configs, dict):
                ctx.fail("project_yml.invalid", rel, settings.line_of("configs"), f"{rel}: configs is not a mapping.")
            else:
                for cname, cset in configs.items():
                    _flat_settings(ctx, rel, cset, configs.line_of(cname), scope, spec)
    else:
        _flat_settings(ctx, rel, settings, line, scope, spec)


def _flat_settings(ctx, rel, flat, line, scope, spec):
    if not isinstance(flat, dict):
        ctx.fail("project_yml.invalid", rel, line, f"{rel}: settings for {scope} is not a mapping.")
        return
    pol = ctx.policy["project_yml"]
    allowed = set(pol["allowed_build_settings"])
    prefixes = tuple(pol["allowed_build_setting_prefixes"])
    for key, value in flat.items():
        kline = flat.line_of(key)
        if isinstance(value, (dict, list)):
            ctx.fail("project_yml.build_setting", rel, kline, f"{rel}: build setting {key!r} is not a plain value.")
            continue
        base_key = str(key).split("[", 1)[0]  # conditional settings: KEY[sdk=...]
        if base_key != key:
            ctx.fail("project_yml.build_setting", rel, kline,
                     f"{rel}: conditional build setting {key!r} is not allowed.")
            continue
        if key not in allowed and not str(key).startswith(prefixes):
            ctx.fail("project_yml.build_setting", rel, kline,
                     f"{rel}: build setting {key!r} is not on the allowed list.")
            continue
        spec.settings.append((key, value, kline, scope))


def _check_settings(ctx, rel, spec: ProjectSpec):
    app_cfg = ctx.policy["app"]
    fixed = ctx.policy["project_yml"]["fixed_build_settings"]
    want_family = str(app_cfg["targeted_device_family"])
    want_dt = str(app_cfg["deployment_target"])
    bundle = ctx.bundle_id
    app = spec.app_target
    app_bundle_seen = False
    family_seen = False
    for key, value, line, scope in spec.settings:
        v = _norm(value)
        if key in fixed and value not in fixed[key] and v not in [_norm(x) for x in fixed[key]]:
            ctx.fail("project_yml.build_setting", rel, line,
                     f"{rel}: {key} is {v!r}; it must be {_norm(fixed[key][0])!r}.")
        if key == "TARGETED_DEVICE_FAMILY":
            if scope in ("project", app):
                family_seen = True
            if v != want_family:
                ctx.fail("project_yml.iphone_only", rel, line,
                         f"{rel}: TARGETED_DEVICE_FAMILY is {v!r} in {scope}; it must be {want_family!r} (iPhone).")
        elif key == "IPHONEOS_DEPLOYMENT_TARGET":
            if v != want_dt:
                ctx.fail("project_yml.deployment_target", rel, line,
                         f"{rel}: IPHONEOS_DEPLOYMENT_TARGET is {v!r} in {scope}, not {want_dt!r}.")
        elif key == "PRODUCT_BUNDLE_IDENTIFIER":
            if scope in ("project", app):
                if scope == app:
                    app_bundle_seen = True
                if v != bundle:
                    ctx.fail("bundle_id.invalid", rel, line,
                             f"{rel}: PRODUCT_BUNDLE_IDENTIFIER is {v!r} in {scope}; the approved bundle ID is {bundle!r}.")
            elif not v.startswith(bundle + "."):
                ctx.fail("bundle_id.invalid", rel, line,
                         f"{rel}: test target {scope} has bundle ID {v!r}; it must start with {bundle + '.'!r}.")
        elif key in ("INFOPLIST_FILE", "CODE_SIGN_ENTITLEMENTS"):
            if not _safe_relpath(value):
                ctx.fail("project_yml.path_escape", rel, line, f"{rel}: {key} {v!r} is not a plain path inside the repo.")
            else:
                purpose = "info" if key == "INFOPLIST_FILE" else "entitlements"
                spec.referenced_paths.setdefault(purpose, []).append((v, line))
    if app is not None and not app_bundle_seen:
        ctx.fail("bundle_id.invalid", rel, None,
                 f"{rel}: the app target {app!r} does not set PRODUCT_BUNDLE_IDENTIFIER to {bundle!r}.")
    if app is not None and not family_seen:
        ctx.fail("project_yml.iphone_only", rel, None,
                 f"{rel}: TARGETED_DEVICE_FAMILY is not set, so XcodeGen defaults to iPhone and iPad.")


def _check_packages(ctx, rel, packages, spec):
    allow = {_norm_url(e["url"]): [str(v) for v in e.get("versions", [])]
             for e in ctx.policy["dependencies"]["allowlist"]}
    for name, pkg in packages.items():
        line = packages.line_of(name)
        if not isinstance(pkg, dict):
            ctx.fail("project_yml.dependency", rel, line, f"{rel}: package {name!r} is not a mapping.")
            continue
        if "url" not in pkg:
            ctx.fail("project_yml.dependency", rel, line,
                     f"{rel}: package {name!r} is a local package; only allowlisted remote packages are allowed.")
            continue
        url = _norm_url(str(pkg["url"]))
        version = pkg.get("exactVersion", pkg.get("version"))
        extra = set(pkg) - {"url", "exactVersion", "version"}
        if extra:
            ctx.fail("project_yml.dependency", rel, line,
                     f"{rel}: package {name!r} uses {sorted(extra)}; pin it with exactVersion only.")
            continue
        if url not in allow:
            ctx.fail("project_yml.dependency", rel, line, f"{rel}: package {name!r} ({pkg['url']}) is not on the allowlist.")
            continue
        if version is None or _norm(version) not in allow[url]:
            ctx.fail("project_yml.dependency", rel, line,
                     f"{rel}: package {name!r} version {version!r} is not an allowed exact version {allow[url]}.")
            continue
        spec.packages[name] = (url, _norm(version), line)


def _norm_url(url: str) -> str:
    u = url.strip().lower().rstrip("/")
    return u[:-4] if u.endswith(".git") else u


def _check_target(ctx, rel, name, target, tline, packages, targets, spec, options_dt):
    pol = ctx.policy["project_yml"]
    app_cfg = ctx.policy["app"]
    for key in target:
        if key not in pol["allowed_target_keys"] and key not in pol["forbidden_keys"]:
            ctx.fail("project_yml.unknown_key", rel, target.line_of(key), f"{rel}: target {name!r} has key {key!r}.")
    ttype = target.get("type")
    if ttype not in app_cfg["target_types"]:
        ctx.fail("project_yml.target_type", rel, target.line_of("type", tline),
                 f"{rel}: target {name!r} has type {ttype!r}, which is not allowed.")
    else:
        spec.target_types[name] = ttype
        if ttype == "application" and spec.app_target is None:
            spec.app_target = name
    platform = target.get("platform")
    if platform != app_cfg["platform"]:
        ctx.fail("project_yml.platform", rel, target.line_of("platform", tline),
                 f"{rel}: target {name!r} platform is {platform!r}, not {app_cfg['platform']!r}.")
    if "supportedDestinations" in target and list(target["supportedDestinations"] or []) != [app_cfg["platform"]]:
        ctx.fail("project_yml.platform", rel, target.line_of("supportedDestinations"),
                 f"{rel}: target {name!r} supportedDestinations must be [{app_cfg['platform']}] only.")
    want_dt = str(app_cfg["deployment_target"])
    if "deploymentTarget" in target:
        dt = _norm(target["deploymentTarget"])
        if dt != want_dt:
            ctx.fail("project_yml.deployment_target", rel, target.line_of("deploymentTarget"),
                     f"{rel}: target {name!r} deploymentTarget is {dt!r}, not {want_dt!r}.")
    elif ttype == "application" and options_dt is None:
        ctx.fail("project_yml.deployment_target", rel, tline,
                 f"{rel}: the app target {name!r} does not set a deployment target, so Xcode picks one.")

    for key in ("sources", "resources"):
        if key not in target:
            continue
        entries = target[key]
        entries = entries if isinstance(entries, list) else [entries]
        lines = getattr(target[key], "lines", None)
        for i, entry in enumerate(entries):
            line = lines[i] if lines and i < len(lines) else target.line_of(key)
            path = entry.get("path") if isinstance(entry, dict) else entry
            if isinstance(entry, dict):
                for k in entry:
                    if k not in pol["allowed_source_entry_keys"] and k not in pol["forbidden_keys"]:
                        ctx.fail("project_yml.unknown_key", rel, entry.line_of(k, line),
                                 f"{rel}: target {name!r} {key} entry {path!r} has key {k!r}.")
            if not _safe_relpath(path):
                ctx.fail("project_yml.path_escape", rel, line,
                         f"{rel}: target {name!r} {key} path {path!r} is not a plain path inside the repo.")

    if "settings" in target:
        _collect_settings(ctx, rel, target["settings"], target.line_of("settings"), name, spec)

    info = target.get("info")
    if info is not None:
        iline = target.line_of("info")
        if not isinstance(info, dict):
            ctx.fail("project_yml.invalid", rel, iline, f"{rel}: target {name!r} info is not a mapping.")
        else:
            _plist_spec_keys(ctx, rel, name, "info", info)
            if "path" in info:
                if not _safe_relpath(info["path"]):
                    ctx.fail("project_yml.path_escape", rel, iline, f"{rel}: info path {info['path']!r} leaves the repo.")
                else:
                    spec.generated_paths.add(fold_path(info["path"]))
            props = info.get("properties") or {}
            if isinstance(props, dict):
                for k, v in props.items():
                    spec.info_properties[k] = (v, props.line_of(k))

    ent = target.get("entitlements")
    if ent is not None:
        eline = target.line_of("entitlements")
        if not isinstance(ent, dict):
            ctx.fail("project_yml.invalid", rel, eline, f"{rel}: target {name!r} entitlements is not a mapping.")
        else:
            _plist_spec_keys(ctx, rel, name, "entitlements", ent)
            if "path" in ent and not _safe_relpath(ent["path"]):
                ctx.fail("project_yml.path_escape", rel, eline, f"{rel}: entitlements path {ent['path']!r} leaves the repo.")
            elif "path" in ent:
                spec.referenced_paths.setdefault("entitlements", []).append((ent["path"], eline))
                spec.generated_paths.add(fold_path(ent["path"]))
            props = ent.get("properties") or {}
            if isinstance(props, dict) and props:
                spec.entitlement_props.append((list(props), eline))

    deps = target.get("dependencies") or []
    if not isinstance(deps, list):
        ctx.fail("project_yml.invalid", rel, target.line_of("dependencies"), f"{rel}: dependencies is not a list.")
        deps = []
    kinds = set(pol["allowed_dependency_kinds"])
    known_kinds = kinds | {"framework", "carthage", "bundle", "sdk", "package", "target"}
    extra_ok = {"product", "link", "embed", "weak", "codeSign"}
    for i, dep in enumerate(deps):
        line = deps.lines[i] if hasattr(deps, "lines") else None
        if not isinstance(dep, dict):
            ctx.fail("project_yml.dependency", rel, line, f"{rel}: target {name!r} dependency {dep!r} is not a mapping.")
            continue
        dep_kinds = [k for k in dep if k in known_kinds]
        others = [k for k in dep if k not in known_kinds and k not in extra_ok]
        if len(dep_kinds) != 1 or others:
            ctx.fail("project_yml.dependency", rel, line,
                     f"{rel}: target {name!r} dependency {dict(dep)} is not one plain package, target or sdk.")
            continue
        kind = dep_kinds[0]
        value = dep[kind]
        if kind not in kinds:
            ctx.fail("project_yml.dependency", rel, line,
                     f"{rel}: target {name!r} depends on a {kind} ({value!r}); only allowlisted packages are allowed.")
        elif kind == "package" and value not in packages:
            ctx.fail("project_yml.dependency", rel, line,
                     f"{rel}: target {name!r} uses package {value!r}, which is not declared under packages.")
        elif kind == "target" and value not in targets:
            ctx.fail("project_yml.dependency", rel, line, f"{rel}: target {name!r} depends on unknown target {value!r}.")
        elif kind == "sdk" and not (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9]+\.(framework|tbd)", value)):
            ctx.fail("project_yml.dependency", rel, line,
                     f"{rel}: target {name!r} sdk dependency {value!r} is not a system framework name.")


def _plist_spec_keys(ctx, rel, name, what, node):
    pol = ctx.policy["project_yml"]
    for k in node:
        if k not in pol["allowed_plist_spec_keys"] and k not in pol["forbidden_keys"]:
            ctx.fail("project_yml.unknown_key", rel, node.line_of(k),
                     f"{rel}: target {name!r} {what} has key {k!r}.")


# --------------------------------------------------------- packages resolved


def check_package_resolved(ctx: Context, spec: ProjectSpec | None):
    rel = ctx.policy["dependencies"]["resolved_path"]
    allow = {_norm_url(e["url"]): [str(v) for v in e.get("versions", [])]
             for e in ctx.policy["dependencies"]["allowlist"]}
    declared = spec.packages if spec else {}
    rel = ctx.find(rel) or rel
    data = ctx.contents.get(rel)
    if data is None:
        if declared:
            ctx.fail("deps.unpinned", rel, None, f"{rel} is missing, so the packages are not pinned.")
        return
    try:
        doc = json.loads(data)
        pins = doc["pins"] if isinstance(doc, dict) else None
        if not isinstance(pins, list):
            raise ValueError("no pins list")
    except (ValueError, KeyError, TypeError) as exc:
        ctx.fail("deps.unpinned", rel, None, f"{rel} could not be read ({exc}).")
        return
    pinned = {}
    for pin in pins:
        if not isinstance(pin, dict):
            ctx.fail("deps.unpinned", rel, None, f"{rel} has a malformed pin.")
            continue
        url = _norm_url(str(pin.get("location") or pin.get("repositoryURL") or ""))
        state = pin.get("state") or {}
        version = str(state.get("version") or "")
        revision = str(state.get("revision") or "")
        if url not in allow:
            ctx.fail("deps.unpinned", rel, None, f"{rel} pins {url or pin!r}, which is not on the allowlist.")
            continue
        if not version or version not in allow[url] or not re.fullmatch(r"[0-9a-f]{40}", revision):
            ctx.fail("deps.unpinned", rel, None,
                     f"{rel} pins {url} at version {version!r} revision {revision!r}; it needs an allowed version and a full commit hash.")
            continue
        pinned[url] = version
    for name, (url, version, line) in declared.items():
        if pinned.get(url) != version:
            ctx.fail("deps.unpinned", rel, None, f"{rel} does not pin package {name!r} at {version}.")


# ------------------------------------------------------------ entitlements


def _read_plist(ctx: Context, rel: str):
    data = ctx.contents.get(rel)
    if data is None:
        return None, "missing"
    try:
        value = plistlib.loads(data)
    except Exception as exc:  # plistlib raises several unrelated types
        return None, f"not a readable property list ({type(exc).__name__})"
    if not isinstance(value, dict):
        return None, "not a dictionary"
    return value, None


def _referenced_files(ctx: Context, spec: ProjectSpec, purpose: str, what: str) -> set:
    """The loaded files project.yml points at for `purpose`, found the way the
    build machine's volume finds them (`./X`, `a//b`, any case). A path that
    names no file in the repo fails, unless XcodeGen generates it."""
    ptl = ctx.policy["project_yml"]["path"]
    found = set()
    for p, line in spec.referenced_paths.get(purpose, []):
        rel = ctx.find(p)
        if rel is not None:
            found.add(rel)
        elif fold_path(p) not in spec.generated_paths:
            ctx.fail("project_yml.path_escape", ptl, line,
                     f"{ptl}: the {what} {p!r} is not a file in the repo the checker could read.")
    return found


def check_entitlements(ctx: Context, spec: ProjectSpec | None):
    allowed = set(ctx.policy["entitlements"]["per_app"].get(ctx.bundle_id, []) or [])
    files = {rel for rel in ctx.contents if rel.lower().endswith(".entitlements")}
    if spec:
        # Whatever the extension: CODE_SIGN_ENTITLEMENTS may name a .plist.
        files |= _referenced_files(ctx, spec, "entitlements", "entitlements file")
        for keys, line in spec.entitlement_props:
            for k in keys:
                if k not in allowed:
                    ctx.fail("entitlements.not_allowed", ctx.policy["project_yml"]["path"], line,
                             f"project.yml asks for entitlement {k!r}, which is not enabled for {ctx.bundle_id}.")
    for rel in sorted(files):
        value, err = _read_plist(ctx, rel)
        if err:
            ctx.fail("entitlements.not_allowed", rel, None, f"{rel} is {err}.")
            continue
        for k in value:
            if k not in allowed:
                ctx.fail("entitlements.not_allowed", rel, None,
                         f"{rel} asks for entitlement {k!r}, which is not enabled for {ctx.bundle_id}.")


# ----------------------------------------------------- usage descriptions


def _declared_plist(ctx: Context, spec: ProjectSpec | None) -> dict:
    """key -> (value, file, line) from settings, info.properties and Info.plist files."""
    out = {}
    ptl = ctx.policy["project_yml"]["path"]
    plists = {rel for rel in ctx.contents if fold_name(rel.rsplit("/", 1)[-1]) == "info.plist"}
    if spec:
        for key, value, line, scope in spec.settings:
            if str(key).startswith("INFOPLIST_KEY_"):
                out[key[len("INFOPLIST_KEY_"):]] = (value, ptl, line)
        for key, (value, line) in spec.info_properties.items():
            out[key] = (value, ptl, line)
        plists |= _referenced_files(ctx, spec, "info", "Info.plist file")
    for rel in sorted(plists):
        value, err = _read_plist(ctx, rel)
        if err:
            ctx.fail("usage_description.missing", rel, None, f"{rel} is {err}.")
            continue
        for k, v in value.items():
            out.setdefault(k, (v, rel, None))
    return out


def check_info_and_usage(ctx: Context, spec: ProjectSpec | None):
    cfg = ctx.policy["usage_descriptions"]
    declared = _declared_plist(ctx, spec)
    bid = declared.get("CFBundleIdentifier")
    if bid is not None and _norm(bid[0]) not in ("$(PRODUCT_BUNDLE_IDENTIFIER)", ctx.bundle_id):
        ctx.fail("bundle_id.invalid", bid[1], bid[2],
                 f"{bid[1]} sets CFBundleIdentifier to {_norm(bid[0])!r}; the approved bundle ID is {ctx.bundle_id!r}.")
    swift = {rel: ctx.text(rel) for rel in ctx.contents if rel.lower().endswith(".swift")}
    needed = {}
    for req in cfg["required"]:
        rx = re.compile(req["api"])
        for rel in sorted(swift):
            m = rx.search(swift[rel])
            if m:
                needed[req["key"]] = (req["what"], rel, _line_of(swift[rel], m.start()))
                break
        if req["key"] in needed or not req.get("api_together"):
            continue
        # `api_together`: every pattern matches in some Swift file (not
        # necessarily the same one); reported where the last one matches.
        where = None
        for pattern in req["api_together"]:
            rx = re.compile(pattern)
            where = next(((rel, m) for rel in sorted(swift) for m in [rx.search(swift[rel])] if m), None)
            if where is None:
                break
        if where is not None:
            rel, m = where
            needed[req["key"]] = (req["what"], rel, _line_of(swift[rel], m.start()))
    for key, (what, rel, line) in sorted(needed.items()):
        value = declared.get(key)
        if value is None or not _norm(value[0]):
            ctx.fail("usage_description.missing", rel, line,
                     f"{rel} uses {what}, but the app does not declare {key}.")
    readme_rel = ctx.find(cfg["readme_path"]) or cfg["readme_path"]
    readme = ctx.text(readme_rel) or ""
    known = {r["key"] for r in cfg["required"]}
    listed = set()
    for key in declared:
        if key in known or re.fullmatch(r"NS\w+UsageDescription", str(key)):
            listed.add(key)
    for key in sorted(listed):
        if key not in readme:
            ctx.fail("usage_description.not_in_readme", readme_rel, None,
                     f"{readme_rel} does not list {key}.")


# ------------------------------------------------- app transport security

ATS_KEY = "NSAppTransportSecurity"


def check_transport_security(ctx: Context, spec: ProjectSpec | None):
    """NSAppTransportSecurity may hold only `infrastructure.ats_allowed_keys`
    (none today), wherever the app's Info.plist is built from: every source is
    checked, not just the one that wins the merge."""
    allowed = set(ctx.policy["infrastructure"]["ats_allowed_keys"] or [])
    ptl = ctx.policy["project_yml"]["path"]
    sources = []  # (value, file, line)
    plists = {rel for rel in ctx.contents if fold_name(rel.rsplit("/", 1)[-1]) == "info.plist"}
    if spec:
        for key, value, line, _ in spec.settings:
            if str(key) == "INFOPLIST_KEY_" + ATS_KEY:
                sources.append((value, ptl, line))
        if ATS_KEY in spec.info_properties:
            value, line = spec.info_properties[ATS_KEY]
            sources.append((value, ptl, line))
        plists |= {ctx.find(p) for p, _ in spec.referenced_paths.get("info", [])} - {None}
        # A target's `info: path:` (XcodeGen writes it; a committed copy is read too).
        plists |= {rel for rel in ctx.contents if fold_path(rel) in spec.generated_paths}
    for rel in sorted(plists):
        value, err = _read_plist(ctx, rel)
        if not err and ATS_KEY in value:
            sources.append((value[ATS_KEY], rel, None))
    for value, file, line in sources:
        if isinstance(value, dict):
            bad = sorted(str(k) for k in value if k not in allowed)
        else:
            bad = [] if value in (None, "") else [f"{ATS_KEY} = {str(value)[:80]!r}"]
        for k in bad:
            ctx.fail("infra.insecure_transport", file, line,
                     f"{file} sets {k} under {ATS_KEY}, which is not allowed.")


# -------------------------------------------------------- export compliance

EXPORT_KEY = "ITSAppUsesNonExemptEncryption"
_FALSE = {"no", "false", "0"}


def _is_false(value) -> bool:
    if isinstance(value, bool):
        return value is False
    return isinstance(value, (str, int)) and str(value).strip().lower() in _FALSE


def check_export_compliance(ctx: Context, spec: ProjectSpec | None):
    """`ITSAppUsesNonExemptEncryption` must be declared false in the app's
    Info.plist sources (the template has it in the target's info properties),
    and no source may say anything else. Without it an upload waits in
    MISSING_EXPORT_COMPLIANCE, and no tester, internal ones included, can
    install it until Andrew answers in App Store Connect (audit 2026-09-19).
    Apple: "a Boolean value indicating whether the app uses encryption"
    (ITSAppUsesNonExemptEncryption, Information Property List)."""
    if spec is None:
        return
    ptl = ctx.policy["project_yml"]["path"]
    sources, declared = [], False  # (value, file, line)
    for key, value, line, _ in spec.settings:
        if str(key) == "INFOPLIST_KEY_" + EXPORT_KEY:
            sources.append((value, ptl, line))
    if EXPORT_KEY in spec.info_properties:
        value, line = spec.info_properties[EXPORT_KEY]
        sources.append((value, ptl, line))
        declared = True
    plists = {ctx.find(p) for p, _ in spec.referenced_paths.get("info", [])} - {None}
    for rel in sorted(plists):
        value, err = _read_plist(ctx, rel)
        if not err and EXPORT_KEY in value:
            sources.append((value[EXPORT_KEY], rel, None))
            declared = True
    for value, file, line in sources:
        if not _is_false(value):
            ctx.fail("project_yml.export_compliance", file, line,
                     f"{file} sets {EXPORT_KEY} to {str(value)[:40]!r}; it must be false.")
    if not declared:
        ctx.fail("project_yml.export_compliance", ptl, None,
                 f"{ptl}: the app target's info properties do not declare {EXPORT_KEY}: false.")


# ------------------------------------------------------------------ app icon

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_COLOR_TYPES = {0: "greyscale", 2: "RGB", 3: "palette colour", 4: "greyscale with transparency",
                   6: "RGB with transparency (an alpha channel)"}


def png_header(data: bytes) -> tuple[dict | None, str | None]:
    """(IHDR fields plus `trns`, None) or (None, why not). Reads the signature,
    the IHDR chunk and every chunk header before the first IDAT, as data."""
    if not data.startswith(PNG_SIGNATURE):
        return None, "is not a PNG"
    pos = len(PNG_SIGNATURE)
    if len(data) < pos + 8 + 13 or data[pos:pos + 8] != b"\x00\x00\x00\x0dIHDR":
        return None, "is a PNG without a readable header"
    w, h = int.from_bytes(data[pos + 8:pos + 12], "big"), int.from_bytes(data[pos + 12:pos + 16], "big")
    fields = {"width": w, "height": h, "bit_depth": data[pos + 16], "color_type": data[pos + 17], "trns": False}
    pos += 8 + 13 + 4
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        if kind == b"IDAT" or kind == b"IEND":
            return fields, None
        if kind == b"tRNS":
            fields["trns"] = True
        pos += 12 + length
    return None, "is a PNG that ends before its image data"


def check_app_icon(ctx: Context):
    pol = ctx.policy["app_icon"]
    size = int(pol["size"])
    color_types = {int(c) for c in pol["color_types"]}
    depths = {int(d) for d in pol["bit_depths"]}
    sets = sorted(rel for rel in ctx.contents
                  if fold_name(rel.rsplit("/", 1)[-1]) == "contents.json" and "/" in rel
                  and fold_name(rel.rsplit("/", 2)[-2]).endswith(".appiconset"))
    for contents_rel in sets:
        set_dir = contents_rel.rsplit("/", 1)[0]
        try:
            doc = json.loads(ctx.contents[contents_rel].decode("utf-8-sig"))
            images = doc.get("images") if isinstance(doc, dict) else None
            if images is None:
                images = []
            if not isinstance(images, list):
                raise ValueError("images is not a list")
        except (ValueError, UnicodeDecodeError) as e:
            ctx.fail("app_icon.invalid", contents_rel, None, f"{contents_rel} could not be read ({str(e)[:80]}).")
            continue
        for entry in images:
            if not isinstance(entry, dict) or "filename" not in entry:
                continue
            name = entry["filename"]
            if not isinstance(name, str) or not name or "/" in name or "\\" in name or name in (".", ".."):
                ctx.fail("app_icon.invalid", contents_rel, None,
                         f"{contents_rel} names an icon file {str(name)[:80]!r} that is not a plain file name.")
                continue
            rel = ctx.find(f"{set_dir}/{name}")
            if rel is None:
                ctx.fail("app_icon.invalid", contents_rel, None,
                         f"{contents_rel} names {name}, which is not in {set_dir}.")
                continue
            header, why = png_header(ctx.contents[rel])
            if header is None:
                ctx.fail("app_icon.invalid", rel, None, f"{rel} {why}.")
                continue
            problems = []
            if (header["width"], header["height"]) != (size, size):
                problems.append(f"is {header['width']}x{header['height']} pixels, not {size}x{size}")
            variant = bool(entry.get("appearances"))
            if not variant:
                if header["color_type"] not in color_types:
                    problems.append("is " + PNG_COLOR_TYPES.get(header["color_type"], "an unknown colour type")
                                    + ", not RGB without transparency")
                elif header["trns"]:
                    problems.append("has a transparent colour (a tRNS chunk)")
                if header["bit_depth"] not in depths:
                    problems.append(f"has {header['bit_depth']}-bit samples")
            if problems:
                ctx.fail("app_icon.invalid", rel, None, f"{rel} " + " and ".join(problems) + ".")
            elif not variant and ctx.app_icon is None:
                ctx.app_icon = rel


# ------------------------------------------------------------------ bundle


def check_bundle_id(ctx: Context):
    prefix = ctx.policy["app"]["bundle_id_prefix"]
    if not (ctx.bundle_id.startswith(prefix)
            and re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9][A-Za-z0-9-]*(\.[A-Za-z0-9][A-Za-z0-9-]*)*", ctx.bundle_id)):
        ctx.fail("bundle_id.invalid", None, None,
                 f"The requested bundle ID {ctx.bundle_id!r} is not under {prefix!r}.")


def run_static_checks(ctx: Context):
    check_bundle_id(ctx)
    walk_files(ctx)
    check_contents(ctx)
    check_infrastructure(ctx)
    spec = check_project_yml(ctx)
    check_package_resolved(ctx, spec)
    check_entitlements(ctx, spec)
    if spec is not None:
        # Without a readable project.yml the declarations are unknown; that is
        # already a hard failure, and guessing here would only add noise.
        check_info_and_usage(ctx, spec)
    check_transport_security(ctx, spec)
    check_export_compliance(ctx, spec)
    check_app_icon(ctx)
    return spec
