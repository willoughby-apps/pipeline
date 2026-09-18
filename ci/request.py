"""Request job (tags only): open the release request and start the review.

    ORG_TOKEN=... MONOREPO_DISPATCH=... REPO SHA TAG RUN_ID RUN_URL IPA_SHA256 \\
        python3 ci/request.py

1. Opens (or reuses) the issue "Release request: <tag>" in the guest's repo,
   labelled `release-request`, pinned to the SHA, the pipeline run and the
   unsigned build's SHA-256. The guest gets GitHub's own email for it; Andrew
   approves or rejects it from the Willoughby TestFlight page (PLAN section 5).
2. Dispatches `guest-review.yml` on ajcohen9/willoughby with the same pins, so
   the advisory review is posted on the issue.

Every value here is either ours or validated (repo name, 40-hex SHA, v1.2.3
tag, numeric run id, 64-hex digest); no guest-written text is used.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ghapi import ORG, Client, GitHubError, validate_inputs  # noqa: E402

LABEL = "release-request"
MONOREPO = "ajcohen9/willoughby"
REVIEW_WORKFLOW = "guest-review.yml"


def issue_title(tag: str) -> str:
    return f"Release request: {tag}"


def issue_body(repo: str, sha: str, tag: str, run_id: str, run_url: str, ipa_sha256: str) -> str:
    return "\n".join([
        f"Version **{tag}** passed the safety checks, compiles and opens in the simulator.",
        "",
        "Andrew approves or rejects it. When he approves, it goes to TestFlight and this issue closes.",
        "",
        "<!-- willoughby-release",
        f"repo: {ORG}/{repo}",
        f"sha: {sha}",
        f"tag: {tag}",
        f"pipeline_run: {run_id}",
        f"unsigned_sha256: {ipa_sha256}",
        "-->",
        f"Commit: {sha}",
        f"Pipeline run: {run_url}",
    ])


def find_open_request(client: Client, repo: str, tag: str) -> dict | None:
    for issue in client.paginate(f"/repos/{ORG}/{repo}/issues?state=open&labels={LABEL}", limit=300):
        if issue.get("title") == issue_title(tag) and "pull_request" not in issue:
            return issue
    return None


def main() -> int:
    repo, sha, tag = os.environ.get("REPO", ""), os.environ.get("SHA", ""), os.environ.get("TAG", "")
    run_id, run_url = os.environ.get("RUN_ID", ""), os.environ.get("RUN_URL", "")
    ipa = os.environ.get("IPA_SHA256", "")
    validate_inputs(repo, sha, "tag", tag)
    if not re.fullmatch(r"[0-9]{1,20}", run_id) or not re.fullmatch(r"[0-9a-f]{64}", ipa):
        raise SystemExit("run id or unsigned build digest missing")
    org = Client.from_env("ORG_TOKEN")
    body = issue_body(repo, sha, tag, run_id, run_url, ipa)
    existing = find_open_request(org, repo, tag)
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
            "inputs": {"repo": f"{ORG}/{repo}", "sha": sha, "tag": tag, "issue": str(number), "run_id": run_id}})
    except GitHubError as e:
        raise SystemExit(f"could not start {REVIEW_WORKFLOW} on {MONOREPO}: HTTP {e.status}") from None
    print(f"dispatched {REVIEW_WORKFLOW}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
