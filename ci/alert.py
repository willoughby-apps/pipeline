"""Tell Andrew that his pipeline could not check a guest's commit.

    alert.notify(org_client, repo, sha, kind, tag, tries)

poll.py calls it when a commit's last try failed on Andrew's side (an `error`
status of ours, or a run that never reported). A check.yml run is started by
GITHUB_TOKEN, and GitHub sends a workflow run's notification only to the
person who triggered it, so a failed run tells nobody (docs, "Notifications
for workflow runs"). This opens one issue in the guest's own repo, as the
bot, labelled `needs-andrew` (so Andrew's Willoughby page lists it) and
@mentioning him in visible text (a mention inside an HTML comment notifies
nobody). A second failure on the same commit while the issue is open is a
comment on it, not a new issue.

Everything written here is ours: the repo name and SHA are validated, the
kind and tag come from poll's own lists. The guest can read the issue, so it
is plain words for them too.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ghapi import BOT_LOGIN, ORG, PIPELINE_REPO, REPO_NAME_RE, SHA_RE, TAG_RE, Client, GitHubError  # noqa: E402

ANDREW = "ajcohen9"
LABEL = "needs-andrew"
# The same colour and text as the reconciler's (onboard/reconcile.py LABELS; a test pins the two).
LABEL_SPEC = ("d93f0b", "Something only Andrew can do")
WHAT = {"push": "check this commit", "tag": "check release {tag}", "testers": "check the new tester list"}


def title(sha: str) -> str:
    return f"Andrew's system could not check {sha[:7]}"


def body(repo: str, sha: str, kind: str, tag: str, tries: int) -> str:
    what = WHAT[kind].format(tag=tag)
    return "\n".join([
        f"@{ANDREW}: the pipeline failed on its side {tries} times trying to {what}, and has stopped trying.",
        "",
        "Nothing is wrong with the app, and there is nothing to change in it. When Andrew has fixed his system, "
        "he starts the check again, and the result appears on the commit as usual.",
        "",
        f"Commit: {sha}",
        f"Runs: https://github.com/{PIPELINE_REPO}/actions/workflows/check.yml",
        f"<!-- willoughby-alert repo={ORG}/{repo} sha={sha} kind={kind} -->",
    ])


def ensure_label(org: Client, repo: str) -> None:
    try:
        have = {lb.get("name") for lb in org.get(f"/repos/{ORG}/{repo}/labels?per_page=100") or []}
    except GitHubError:
        have = set()
    if LABEL in have:
        return
    color, desc = LABEL_SPEC
    try:
        org.post(f"/repos/{ORG}/{repo}/labels", {"name": LABEL, "color": color, "description": desc})
    except GitHubError as e:
        if e.status != 422:  # already_exists
            raise


def notify(org: Client, repo: str, sha: str, kind: str, tag: str, tries: int) -> int:
    """Open (or add to) the issue; returns its number. Raises GitHubError when
    GitHub refuses, so the caller can try again on its next run."""
    if not (REPO_NAME_RE.fullmatch(repo) and SHA_RE.fullmatch(sha) and kind in WHAT
            and (kind != "tag" or TAG_RE.fullmatch(tag or ""))):
        raise ValueError("refusing to report an unvalidated commit")
    ensure_label(org, repo)
    want = title(sha)
    for issue in org.paginate(f"/repos/{ORG}/{repo}/issues?state=open&labels={LABEL}", limit=300):
        if (issue.get("title") == want and "pull_request" not in issue
                and (issue.get("user") or {}).get("login") == BOT_LOGIN):
            org.post(f"/repos/{ORG}/{repo}/issues/{issue['number']}/comments",
                     {"body": f"@{ANDREW}: it failed again ({tries} tries, {WHAT[kind].format(tag=tag)})."})
            return issue["number"]
    return org.post(f"/repos/{ORG}/{repo}/issues",
                    {"title": want, "body": body(repo, sha, kind, tag, tries), "labels": [LABEL]})["number"]
