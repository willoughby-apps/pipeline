"""Request job (tags only): open the release request and start the review.

    APP_TOKEN=... MONOREPO_DISPATCH=... REPO SHA TAG RUN_ID RUN_URL IPA_SHA256 \\
        python3 ci/request.py

1. Opens (or reuses) the issue "Release request: <tag>" in the guest's repo,
   labelled `release-request`, pinned to the SHA, the pipeline run and the
   unsigned build's SHA-256. The guest gets GitHub's own email for it; Andrew
   approves or rejects it from the Willoughby TestFlight page (PLAN section 5).
2. Dispatches `guest-review.yml` on ajcohen9/willoughby with the same pins, so
   the advisory review is posted on the issue.

The body @mentions ANDREW, so GitHub notifies him (he owns the org, so he can
read every guest repo), and carries the check's images from the previews ref
(`refs/willoughby/previews`, written by report.py): shown in the body and,
for the Willoughby page, in a machine-readable block

    <!-- willoughby-previews v1
    {"sha": ..., "tag": ..., "previews_commit": ...,
     "screenshot": {"url": ..., "api": ...} | null, "icon": {...} | null}
    -->

Every value in it is ours (the report job's commit SHA and our own paths, both
validated here), and like the pin block it is for display: a guest can edit
the body, so a reader checks the author and edit history before trusting it.

Only an issue the pipeline's bot (BOT_LOGIN) opened is reused. A guest has write on
their repo, so they can open "Release request: v1.1" with the label before
tagging; reusing it would put our pins into an issue whose body they can edit
afterwards. The pin block is for people to read: a guest with write can edit
any issue body in their repo, even ours, so nothing may take pins from it
(PLAN sections 4 and 5: consumers read the `willoughby/release` status our user
created, plus the run).

Every value here is either ours or validated (repo name, 40-hex SHA, v1.2.3
tag, numeric run id, 64-hex digests); no guest-written text is used.
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
               source_sha256: str, pv: dict | None = None) -> str:
    return "\n".join([
        f"Version **{tag}** passed the safety checks, compiles and opens in the simulator.",
        "",
        f"@{ANDREW}: this is waiting for your approval in Willoughby.",
        "",
        "Andrew approves or rejects it. When he approves, it goes to TestFlight and this issue closes.",
        "",
        *previews_lines(sha, tag, pv),
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


def find_open_request(client: Client, repo: str, tag: str, login: str) -> dict | None:
    """Our own open request issue for `tag`. An issue anyone else opened (the
    guest can, with the same title and label) is never reused."""
    for issue in client.paginate(f"/repos/{ORG}/{repo}/issues?state=open&labels={LABEL}", limit=300):
        if (issue.get("title") == issue_title(tag) and "pull_request" not in issue
                and (issue.get("user") or {}).get("login") == login):
            return issue
    return None


def main() -> int:
    repo, sha, tag = os.environ.get("REPO", ""), os.environ.get("SHA", ""), os.environ.get("TAG", "")
    run_id, run_url = os.environ.get("RUN_ID", ""), os.environ.get("RUN_URL", "")
    ipa, source = os.environ.get("IPA_SHA256", ""), os.environ.get("SOURCE_SHA256", "")
    pv = previews(repo, sha, os.environ.get("PREVIEWS_COMMIT", ""), os.environ.get("PREVIEW_SCREENSHOT", ""),
                  os.environ.get("PREVIEW_ICON", ""))
    validate_inputs(repo, sha, "tag", tag)
    if not re.fullmatch(r"[0-9]{1,20}", run_id) or not re.fullmatch(r"[0-9a-f]{64}", ipa) \
            or not re.fullmatch(r"[0-9a-f]{64}", source):
        raise SystemExit("run id, unsigned build digest or source digest missing")
    org = Client.from_env(APP_TOKEN_ENV)
    login = BOT_LOGIN  # an installation token cannot call GET /user
    body = issue_body(repo, sha, tag, run_id, run_url, ipa, source, pv)
    existing = find_open_request(org, repo, tag, login)
    if existing:
        # Same tag re-checked (say, after an expired artifact): re-pin the same issue.
        org.json("PATCH", f"/repos/{ORG}/{repo}/issues/{existing['number']}", {"body": body})
        number = existing["number"]
    else:
        number = org.post(f"/repos/{ORG}/{repo}/issues",
                          {"title": issue_title(tag), "body": body, "labels": [LABEL]})["number"]
    print(f"release request is issue #{number}")
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
