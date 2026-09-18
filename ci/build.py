"""Build job: compile a gated guest repo, unsigned, and never print what it says.

    python3 ci/build.py REPO_DIR OUT_DIR

Runs `xcodegen generate`, then `xcodebuild archive` for a device
(CODE_SIGNING_ALLOWED=NO) and `xcodebuild build` for the simulator. Every tool's
output goes to OUT_DIR/build.log, never to this public log. Writes:

    OUT_DIR/result.json        {"ok", "scheme", "stage", "errors": [...]}
    OUT_DIR/unsigned.ipa       Payload/<App>.app, for the Mini to re-sign
    OUT_DIR/sim/<App>.app      the simulator build, for the preview job

No guest code runs here. The gate has already refused build scripts, build
rules, package plugins, macros and every file kind that a compiler would treat
as something other than Swift, assets or text; this job only compiles. Running
the app is the preview job's, on a separate machine, so nothing the app does
can touch the archive recorded here.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

TIMEOUT = 40 * 60
# The compiler's own error line: "path:line:col: error: message".
ERROR_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?:\d+:)? error: (?P<msg>.+)$", re.M)
PLAIN_ERROR_RE = re.compile(r"^(?:error|xcodebuild: error): (?P<msg>.+)$", re.M)
MAX_ERRORS = 30


def compile_errors(log: str, repo_dir: str) -> list[dict]:
    """Compiler errors, with paths made relative to the repo, de-duplicated, capped."""
    out, seen = [], set()
    prefix = str(Path(repo_dir).resolve()) + "/"
    for m in ERROR_RE.finditer(log):
        f = m.group("file")
        f = f[len(prefix):] if f.startswith(prefix) else Path(f).name
        key = (f, m.group("line"), m.group("msg"))
        if key not in seen:
            seen.add(key)
            out.append({"file": f, "line": int(m.group("line")), "message": m.group("msg")[:500]})
    for m in PLAIN_ERROR_RE.finditer(log):
        key = (None, None, m.group("msg"))
        if key not in seen:
            seen.add(key)
            out.append({"file": None, "line": None, "message": m.group("msg")[:500]})
    return out[:MAX_ERRORS]


def pick_scheme(listing: dict) -> str:
    """The one application scheme xcodebuild lists for the generated project."""
    project = listing.get("project") or {}
    schemes = project.get("schemes") or []
    targets = project.get("targets") or []
    candidates = [s for s in schemes if s in targets] or schemes
    if len(candidates) != 1:
        raise ValueError(f"expected one app scheme, found {len(candidates)}")
    return candidates[0]


def _run(argv, cwd, log) -> int:
    log.write(f"\n$ {' '.join(argv)}\n")
    log.flush()
    try:
        return subprocess.run(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        log.write(f"\n(timed out after {TIMEOUT} s)\n")
        return 124


def build(repo_dir: Path, out: Path) -> dict:
    repo_dir, out = repo_dir.resolve(), out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    derived = out.parent / "derived"
    result = {"ok": False, "scheme": None, "stage": "xcodegen", "errors": []}
    log_path = out / "build.log"
    with open(log_path, "w") as log:
        if _run(["xcodegen", "generate", "--spec", "project.yml"], repo_dir, log) != 0:
            return _finish(result, log_path, repo_dir, out)
        projects = sorted(repo_dir.glob("*.xcodeproj"))
        if len(projects) != 1:
            log.write(f"expected one generated .xcodeproj, found {len(projects)}\n")
            return _finish(result, log_path, repo_dir, out)
        project = projects[0].name
        # The gate requires Package.resolved at the repo root; Xcode reads it
        # from inside the project, so put it where Xcode resolves from.
        resolved = repo_dir / "Package.resolved"
        if resolved.is_file() and not resolved.is_symlink():
            dest = projects[0] / "project.xcworkspace/xcshareddata/swiftpm/Package.resolved"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(resolved, dest)
        result["stage"] = "list"
        listing = subprocess.run(["xcodebuild", "-list", "-json", "-project", project], cwd=repo_dir,
                                 capture_output=True, text=True, timeout=600)
        log.write(listing.stdout + listing.stderr)
        try:
            scheme = pick_scheme(json.loads(listing.stdout))
        except (ValueError, json.JSONDecodeError) as e:
            log.write(f"\nerror: {e}\n")
            return _finish(result, log_path, repo_dir, out)
        result["scheme"] = scheme
        common = ["-project", project, "-scheme", scheme, "-derivedDataPath", str(derived),
                  "-onlyUsePackageVersionsFromResolvedFile", "CODE_SIGNING_ALLOWED=NO",
                  "CODE_SIGNING_REQUIRED=NO", "CODE_SIGN_IDENTITY="]
        result["stage"] = "archive"
        archive = out.parent / "App.xcarchive"
        if _run(["xcodebuild", "archive", *common, "-configuration", "Release",
                 "-destination", "generic/platform=iOS", "-archivePath", str(archive)], repo_dir, log) != 0:
            return _finish(result, log_path, repo_dir, out)
        apps = sorted((archive / "Products/Applications").glob("*.app"))
        if len(apps) != 1:
            log.write(f"\nerror: expected one .app in the archive, found {len(apps)}\n")
            return _finish(result, log_path, repo_dir, out)
        payload = out.parent / "ipa"
        (payload / "Payload").mkdir(parents=True, exist_ok=True)
        shutil.copytree(apps[0], payload / "Payload" / apps[0].name, symlinks=True)
        if _run(["ditto", "-c", "-k", "--norsrc", "--noextattr", "--keepParent", "Payload",
                 str(out / "unsigned.ipa")], payload, log) != 0:
            return _finish(result, log_path, repo_dir, out)
        result["stage"] = "simulator"
        if _run(["xcodebuild", "build", *common, "-configuration", "Debug",
                 "-destination", "generic/platform=iOS Simulator"], repo_dir, log) != 0:
            return _finish(result, log_path, repo_dir, out)
        sims = sorted((derived / "Build/Products/Debug-iphonesimulator").glob("*.app"))
        if len(sims) != 1:
            log.write(f"\nerror: expected one simulator .app, found {len(sims)}\n")
            return _finish(result, log_path, repo_dir, out)
        (out / "sim").mkdir(exist_ok=True)
        shutil.copytree(sims[0], out / "sim" / sims[0].name, symlinks=True)
        result["stage"] = "done"
        result["ok"] = True
    return _finish(result, log_path, repo_dir, out)


def _finish(result: dict, log_path: Path, repo_dir: Path, out: Path) -> dict:
    if not result["ok"]:
        result["errors"] = compile_errors(log_path.read_text(errors="replace"), str(repo_dir))
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    result = build(Path(argv[0]), Path(argv[1]))
    # Stage names are ours; nothing from the guest's code reaches this line.
    print(f"build {'succeeded' if result['ok'] else 'failed'} at stage {result['stage']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
