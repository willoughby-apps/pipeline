"""Report job: tell the guest's commit what happened.

    APP_TOKEN=... REPO SHA KIND TAG RUN_URL GATE_PASSED BUILT IPA_SHA256 \\
        python3 ci/report.py REPORTS_DIR

REPORTS_DIR holds what the gate, build and preview jobs encrypted to the
pipeline key, already decrypted by the workflow:

    gate/gate.json, gate/gate.txt, gate/unpack.txt   (gate.json's app_icon: the icon shown)
    build/result.json, build/build.log
    preview/result.json, preview/screenshot.png, preview/preview.log

Everything in there is guest-derived (the preview ran the guest's app), so it
is parsed as data and only ever written to the guest's own private repo: a
commit status, a commit comment, and the screenshot committed under the
non-branch ref `refs/willoughby/previews` (no branch, so the guest's clone and
the poll never see it), beside a copy of the app icon the gate passed (the same
git blob, no bytes re-uploaded). Nothing guest-written reaches this public log.

Writes to $GITHUB_OUTPUT: `ok=true|false`, and for the request job
`previews_commit` (the previews-ref commit holding this check's images),
`preview_screenshot` and `preview_icon` (their paths there, each empty when
not stored). Those paths are ours (`previews/<sha>-<kind>.png`,
`previews/<sha>-<kind>-icon.png`), never the guest's file names, so they can
travel through a job output and a step's env without putting anything
guest-written in the public log.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ghapi import APP_TOKEN_ENV, ORG, Client, GitHubError, validate_inputs  # noqa: E402

CONTEXTS = {"push": "willoughby/check", "tag": "willoughby/release"}
PREVIEW_REF = "refs/willoughby/previews"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_PNG_BYTES = 10 * 1024 * 1024
MAX_COMMENT_CHARS = 60000


def _load_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# The preview job ran the guest's app, which could rewrite its result.json.
# Nothing from it reaches the markdown outside a code fence as written: the
# stage becomes one of our own words and the device must look like a simulator
# name, or the comment (posted as willoughby-apps-bot, Andrew's pipeline) could
# carry an @mention or a link in his voice.
PREVIEW_STAGES = {"device": "finding a simulator", "boot": "starting the simulator",
                  "install": "installing the app", "launch": "opening the app",
                  "screenshot": "taking the screenshot", "done": "done"}
BUILD_STAGES = {"xcodegen": "generating the project", "list": "reading the project",
                "archive": "building for iPhone", "simulator": "building for the simulator", "done": "done"}
DEVICE_RE = re.compile(r"iPhone(?: [A-Za-z0-9]{1,12}){0,4}")


def _stage(value, known: dict) -> str:
    return known.get(value, "an unknown step") if isinstance(value, str) else "an unknown step"


def _device(value) -> str:
    return value if isinstance(value, str) and DEVICE_RE.fullmatch(value) else "iPhone"


def _text(value, limit=400) -> str:
    """Guest-derived text for inside a fenced block: one line, no fence breaks."""
    s = str(value if value is not None else "")
    s = s.replace("\r", " ").replace("\n", " ").replace("```", "'''")
    return s[:limit]


def verdict(env: dict, reports: Path) -> dict:
    """{state, description, headline, sections[]} from the job results and reports."""
    gate = _load_json(reports / "gate/gate.json")
    build = _load_json(reports / "build/result.json")
    preview = _load_json(reports / "preview/result.json")
    sections = []
    if env.get("GATE_PASSED") != "true":
        if gate is not None and gate.get("passed") is False:
            fails = gate.get("hard_failures") or []
            lines = []
            for f in fails[:40]:
                where = _text(f.get("file") or "(repo)", 200)
                if f.get("line"):
                    where += f":{int(f['line'])}"
                lines.append(f"[{_text(f.get('rule'), 60)}] {where}\n  {_text(f.get('plain_english'))}\n"
                             f"  Fix: {_text(f.get('fix_for_claude'))}")
            sections.append(("Safety checks that blocked this commit", "\n".join(lines)))
            return {"state": "failure", "description": f"Blocked by {len(fails)} safety check(s).",
                    "headline": "Blocked: this commit did not pass the safety checks.", "sections": sections}
        unpack = (reports / "gate/unpack.txt")
        if unpack.is_file() and unpack.read_text(errors="replace").strip():
            sections.append(("Why the repo could not be read", _text(unpack.read_text(errors="replace"), 2000)))
            return {"state": "failure", "description": "Blocked: the repo could not be unpacked safely.",
                    "headline": "Blocked: the repo could not be unpacked safely (for example a link that points outside it).",
                    "sections": sections}
        return {"state": "error", "description": "The safety checks could not run (a pipeline problem).",
                "headline": "The safety checks could not run. This is a problem with the pipeline, not with your code: push again later, or ask Andrew with /willoughby-apps:help if it keeps happening.",
                "sections": sections}
    if env.get("BUILT") != "true":
        if build is not None:
            errs = build.get("errors") or []
            lines = []
            for e in errs:
                where = _text(e.get("file") or "", 200)
                if e.get("line"):
                    where += f":{int(e['line'])}"
                lines.append(f"{where + ': ' if where else ''}{_text(e.get('message'))}")
            sections.append(("Compile errors", "\n".join(lines) or f"(failed at {_stage(build.get('stage'), BUILD_STAGES)}; no error lines found)"))
            return {"state": "failure", "description": "Did not compile.",
                    "headline": "Passed the safety checks, but the app did not compile.", "sections": sections}
        return {"state": "error", "description": "The build could not run (a pipeline problem).",
                "headline": "The build could not run. This is a problem with the pipeline, not with your code: push again later, or ask Andrew with /willoughby-apps:help if it keeps happening.", "sections": sections}
    if not (preview and preview.get("ok") is True):
        stage = _stage((preview or {}).get("stage"), PREVIEW_STAGES)
        return {"state": "failure", "description": "Compiles, but did not open in the simulator.",
                "headline": f"Compiles, but the app did not open in the iPhone simulator (stopped at: {stage}).",
                "sections": sections}
    ipa = env.get("IPA_SHA256", "")
    desc = "Passed: builds and opens." + (f" unsigned sha256 {ipa}" if len(ipa) == 64 else "")
    return {"state": "success", "description": desc,
            "headline": f"Passed: it builds and opens on an {_device(preview.get('device'))} simulator.",
            "sections": sections}


# The icon's path comes from gate.json, which names a file in the guest repo:
# only plain path characters reach the comment, each segment URL-quoted, never
# `..`, so it cannot close the HTML attribute or point anywhere else.
ICON_PATH_RE = re.compile(r"[A-Za-z0-9._ /-]{1,300}")


def icon_path(reports: Path) -> str | None:
    gate = _load_json(reports / "gate/gate.json")
    path = (gate or {}).get("app_icon")
    if not (isinstance(path, str) and ICON_PATH_RE.fullmatch(path) and path.lower().endswith(".png")):
        return None
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return path


def icon_links(repo: str, sha: str, path: str) -> tuple[str, str]:
    """(an image URL for the comment, the `gh api` path Claude fetches it with)."""
    from urllib.parse import quote
    q = "/".join(quote(p) for p in path.split("/"))
    return (f"https://github.com/{ORG}/{repo}/blob/{sha}/{q}?raw=true",
            f"repos/{ORG}/{repo}/contents/{q}?ref={sha}")


def comment_body(v: dict, env: dict, image_url: str | None, image_api: str | None,
                 icon: tuple[str, str] | None = None) -> str:
    kind = env.get("KIND")
    title = f"Release check for {env.get('TAG')}" if kind == "tag" else "Check"
    parts = [f"**{title}: {v['headline']}**", ""]
    for heading, body in v["sections"]:
        parts += [f"{heading}:", "```text", body, "```", ""]
    if icon and image_url:
        parts += ["The app icon and a screenshot from the iPhone simulator:", "",
                  "| App icon | Screenshot |", "|:---:|:---:|",
                  f'| <img src="{icon[0]}" width="120" alt="app icon"> '
                  f'| <img src="{image_url}" width="300" alt="screenshot"> |', ""]
    elif icon:
        parts += ["The app icon:", "", f'<img src="{icon[0]}" width="120" alt="app icon">', ""]
    elif image_url:
        parts += ["Screenshot from the iPhone simulator:", "", f"![screenshot]({image_url})", ""]
    if image_url:
        parts += [f"Claude can fetch it with: `gh api {image_api} -H 'Accept: application/vnd.github.raw' > screenshot.png`", ""]
    if icon:
        parts += [f"And the icon with: `gh api {icon[1]} -H 'Accept: application/vnd.github.raw' > icon.png`", ""]
    parts += [f"Pipeline run: {env.get('RUN_URL')}"]
    return "\n".join(parts)[:MAX_COMMENT_CHARS]


def screenshot_bytes(reports: Path) -> bytes | None:
    p = reports / "preview/screenshot.png"
    if not p.is_file() or p.is_symlink():
        return None
    data = p.read_bytes()
    if not data.startswith(PNG_MAGIC) or len(data) > MAX_PNG_BYTES:
        return None
    return data


def preview_paths(sha: str, kind: str) -> dict:
    """Where this check's images live under PREVIEW_REF (ours, never guest-named)."""
    return {"screenshot": f"previews/{sha}-{kind}.png", "icon": f"previews/{sha}-{kind}-icon.png"}


BLOB_SHA_RE = re.compile(r"[0-9a-f]{40}")


def icon_blob(client: Client, repo: str, sha: str, path: str) -> str | None:
    """The git blob SHA of the icon the gate passed, at `sha` (a file, not a link)."""
    from urllib.parse import quote
    q = "/".join(quote(p) for p in path.split("/"))
    try:
        doc = client.get(f"/repos/{ORG}/{repo}/contents/{q}?ref={sha}")
    except GitHubError:
        return None
    if not isinstance(doc, dict) or doc.get("type") != "file":
        return None
    blob = doc.get("sha") or ""
    return blob if BLOB_SHA_RE.fullmatch(blob) else None


def store_screenshot(client: Client, repo: str, sha: str, kind: str, png: bytes) -> tuple[str, str]:
    """Commit the PNG under PREVIEW_REF; returns that commit's SHA and the file path."""
    commit, paths = store_previews(client, repo, sha, kind, png, None)
    return commit, paths["screenshot"]


def store_previews(client: Client, repo: str, sha: str, kind: str, png: bytes | None,
                   icon: str | None) -> tuple[str, dict]:
    """Commit the screenshot PNG and the icon (an existing blob of the repo) under
    PREVIEW_REF in one commit; returns that commit's SHA and {name: path} of
    what it holds."""
    names = preview_paths(sha, kind)
    entries, stored = [], {}
    if png:
        blob = client.post(f"/repos/{ORG}/{repo}/git/blobs",
                           {"content": base64.b64encode(png).decode(), "encoding": "base64"})["sha"]
        entries.append({"path": names["screenshot"], "mode": "100644", "type": "blob", "sha": blob})
        stored["screenshot"] = names["screenshot"]
    if icon:
        entries.append({"path": names["icon"], "mode": "100644", "type": "blob", "sha": icon})
        stored["icon"] = names["icon"]
    if not entries:
        raise RuntimeError("nothing to store")
    what = " and ".join(k for k in ("screenshot", "icon") if k in stored)
    for _ in range(3):
        try:
            parent = client.get(f"/repos/{ORG}/{repo}/git/ref/{PREVIEW_REF[5:]}")["object"]["sha"]
        except GitHubError as e:
            if e.status != 404:
                raise
            parent = None
        body = {"tree": entries}
        if parent:
            body["base_tree"] = client.get(f"/repos/{ORG}/{repo}/git/commits/{parent}")["tree"]["sha"]
        tree = client.post(f"/repos/{ORG}/{repo}/git/trees", body)["sha"]
        commit = client.post(f"/repos/{ORG}/{repo}/git/commits", {
            "message": f"Simulator {what} for {sha[:7]} ({kind})", "tree": tree,
            "parents": [parent] if parent else []})["sha"]
        try:
            if parent:
                client.json("PATCH", f"/repos/{ORG}/{repo}/git/refs/{PREVIEW_REF[5:]}",
                            {"sha": commit, "force": False})
            else:
                client.post(f"/repos/{ORG}/{repo}/git/refs", {"ref": PREVIEW_REF, "sha": commit})
            return commit, stored
        except GitHubError as e:
            if e.status not in (409, 422):  # someone else moved the ref: retry on top of theirs
                raise
    raise RuntimeError("could not update the previews ref after 3 tries")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    env = dict(os.environ)
    repo, sha, kind, tag = env.get("REPO", ""), env.get("SHA", ""), env.get("KIND", ""), env.get("TAG", "")
    validate_inputs(repo, sha, kind, tag)
    reports = Path(argv[0])
    client = Client.from_env(APP_TOKEN_ENV)
    v = verdict(env, reports)
    image_url = image_api = None
    png = screenshot_bytes(reports)
    path = icon_path(reports)
    blob = icon_blob(client, repo, sha, path) if path else None
    previews_commit, stored = "", {}
    if png or blob:
        try:
            previews_commit, stored = store_previews(client, repo, sha, kind, png, blob)
        except (GitHubError, RuntimeError) as e:
            print(f"warning: previews not stored ({type(e).__name__})")
    if "screenshot" in stored:
        image_url = f"https://github.com/{ORG}/{repo}/blob/{previews_commit}/{stored['screenshot']}?raw=true"
        image_api = f"repos/{ORG}/{repo}/contents/{stored['screenshot']}?ref={previews_commit}"
    client.post(f"/repos/{ORG}/{repo}/statuses/{sha}", {
        "state": v["state"], "context": CONTEXTS[kind], "description": v["description"][:140],
        "target_url": env.get("RUN_URL")})
    icon = icon_links(repo, sha, path) if path else None
    client.post(f"/repos/{ORG}/{repo}/commits/{sha}/comments",
                {"body": comment_body(v, env, image_url, image_api, icon)})
    ok = v["state"] == "success"
    out = env.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"ok={'true' if ok else 'false'}\n")
            fh.write(f"previews_commit={previews_commit}\n")
            fh.write(f"preview_screenshot={stored.get('screenshot', '')}\n")
            fh.write(f"preview_icon={stored.get('icon', '')}\n")
    print(f"reported {v['state']} on the commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
