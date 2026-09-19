"""Poll: start check.yml for every new branch head and v* tag in the guest repos.

    APP_TOKEN=... python3 ci/poll.py --list-repos      # writes repos=a,b to $GITHUB_OUTPUT
    APP_TOKEN=... GITHUB_TOKEN=... python3 ci/poll.py [--dry-run]

poll.yml runs it twice, each with its own app token: first a token with only
organization custom properties read and metadata read lists
the guest repos, then a token limited to exactly those repos (contents read,
statuses write, organization custom properties read) polls them. Neither can
reach `pipeline` with any write permission.

A guest repo is any org repo with a `bundle_id` custom property (onboarding
sets it with `guest`; only an org owner can). For each branch head and each
`v<major>[.<minor>[.<patch>]]` tag, a commit that carries no status of ours in
that kind's context gets one check: this dispatches check.yml (a
workflow_dispatch from GITHUB_TOKEN does start a run, per GitHub's docs) and
immediately marks the commit `pending`, which is what stops the next poll from
dispatching it again.

"Ours" means created by the pipeline's bot (ghapi.BOT_LOGIN): a guest can
write statuses on their repo, but not as the bot.

Per guest, at most DAILY_CAP checks start per UTC day, counted from check.yml's
own runs (their run-name carries the repo). Past the cap the commit gets a
`pending` "Waiting" status and is picked up the next day.

**Testers requests (PLAN section 1, 2026-09-19 decision c).** A guest's
tester change takes effect only when Andrew approves it, so poll never hands
a pushed `testers.txt` to the Mini. Instead, for each guest repo whose
default-branch head H differs from A, the last commit Andrew approved (the
repo's org-owned `approved_sha` custom property, which his reconciler sets and
a collaborator cannot), GitHub's compare of A...H is read: when H is ahead of
A and the only files changed are `testers.txt` (plus Andrew's managed files),
check.yml runs at H with kind `testers` (gate and change summary, nothing
built), which opens a "Testers request" for Andrew. H is marked `pending` in
`willoughby/testers-request` first-come like any check, and it counts
towards the daily cap. Anything else that changed means the change waits for
a release, which Andrew approves as a whole.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ghapi import (APP_TOKEN_ENV, BOT_LOGIN, ORG, PIPELINE_REPO, REPO_NAME_RE, RESERVED_REPOS, SHA_RE, TAG_RE,  # noqa: E402
                   Client, GitHubError)

DAILY_CAP = 20
CONTEXTS = {"push": "willoughby/check", "tag": "willoughby/release", "testers": "willoughby/testers-request"}
TESTERS_FILE = "testers.txt"
# A change of only these since the approved commit is a testers request (the
# Mini's common/approvals.py TESTERS_ONLY_PATHS; a test pins the two).
TESTERS_ONLY_PATHS = ("testers.txt", ".claude/settings.json", "CLAUDE.md")
WAITING = "Waiting:"
MAX_BRANCHES = 30
MAX_TAGS = 30


def repo_properties(org: Client) -> dict[str, dict]:
    """{repo: its custom properties} for every repo onboarding labelled."""
    out = {}
    for row in org.paginate(f"/orgs/{ORG}/properties/values"):
        props = {p["property_name"]: p["value"] for p in row.get("properties", [])}
        name = row.get("repository_name", "")
        if props.get("bundle_id") and REPO_NAME_RE.fullmatch(name) and name not in RESERVED_REPOS:
            out[name] = props
    return out


def guest_repos(org: Client, props: dict | None = None) -> dict[str, str]:
    """{repo: guest} for every repo onboarding labelled."""
    props = repo_properties(org) if props is None else props
    return {name: p.get("guest") or name.split("-", 1)[0] for name, p in props.items()}


def candidates(org: Client, repo: str) -> list[tuple[str, str, str]]:
    """(sha, kind, tag) for each branch head and release tag; tag is '' for a head."""
    seen, out = set(), []
    for b in org.paginate(f"/repos/{ORG}/{repo}/branches", limit=MAX_BRANCHES):
        sha = b["commit"]["sha"]
        if SHA_RE.fullmatch(sha) and (sha, "push") not in seen:
            seen.add((sha, "push"))
            out.append((sha, "push", ""))
    for t in org.paginate(f"/repos/{ORG}/{repo}/tags", limit=MAX_TAGS):
        sha = t["commit"]["sha"]
        if TAG_RE.fullmatch(t.get("name", "")) and SHA_RE.fullmatch(sha):
            out.append((sha, "tag", t["name"]))
    return out


def our_status(statuses: list[dict], context: str, login: str) -> dict | None:
    """The newest status in `context` created by `login` (the API lists newest first)."""
    for s in statuses:
        if s.get("context") == context and (s.get("creator") or {}).get("login") == login:
            return s
    return None


def needs_check(statuses: list[dict], context: str, login: str) -> bool:
    s = our_status(statuses, context, login)
    return s is None or (s.get("state") == "pending" and (s.get("description") or "").startswith(WAITING))


def run_name(repo: str, kind: str, sha: str) -> str:
    """Must match check.yml's `run-name`."""
    return f"check {repo} {kind} {sha}"


def checks_today(pipeline: Client, repos: dict[str, str], today: dt.date) -> dict[str, int]:
    """Checks started today per guest, from check.yml's run names."""
    counts: dict[str, int] = {}
    runs = pipeline.paginate(
        f"/repos/{PIPELINE_REPO}/actions/workflows/check.yml/runs?created=>={today.isoformat()}",
        key="workflow_runs", limit=2000)
    for r in runs:
        parts = (r.get("display_title") or "").split(" ")
        if len(parts) == 4 and parts[0] == "check" and parts[1] in repos:
            guest = repos[parts[1]]
            counts[guest] = counts.get(guest, 0) + 1
    return counts


def testers_request(org: Client, repo: str, approved: str) -> str | None:
    """H, the default-branch head, when it is ahead of the approved commit and
    the only files changed are the tester list (and Andrew's managed files)."""
    if not SHA_RE.fullmatch(approved or ""):
        return None
    default = (org.get(f"/repos/{ORG}/{repo}") or {}).get("default_branch") or ""
    if not default:
        return None
    head = ((org.get(f"/repos/{ORG}/{repo}/branches/{default}") or {}).get("commit") or {}).get("sha") or ""
    if not SHA_RE.fullmatch(head) or head == approved:
        return None
    cmp = org.get(f"/repos/{ORG}/{repo}/compare/{approved}...{head}") or {}
    files = {f.get("filename") or "" for f in cmp.get("files") or []}
    if cmp.get("status") != "ahead" or len(cmp.get("files") or []) >= 300:
        return None
    if TESTERS_FILE not in files or not files <= set(TESTERS_ONLY_PATHS):
        return None
    return head


def poll(org: Client, pipeline: Client, today: dt.date, dry_run: bool = False) -> list[tuple]:
    login = BOT_LOGIN  # an installation token cannot call GET /user
    props = repo_properties(org)
    repos = guest_repos(org, props)
    counts = checks_today(pipeline, repos, today)
    actions = []
    for repo in sorted(repos):
        guest = repos[repo]
        found = candidates(org, repo)
        try:
            head = testers_request(org, repo, props[repo].get("approved_sha") or "")
        except GitHubError as e:
            # One repo's lookup failing (say, an empty repo) never stops the checks.
            print(f"testers: {repo} skipped (HTTP {e.status})")
            head = None
        if head:
            found.append((head, "testers", ""))
        for sha, kind, tag in found:
            context = CONTEXTS[kind]
            statuses = org.paginate(f"/repos/{ORG}/{repo}/commits/{sha}/statuses", limit=100)
            if not needs_check(statuses, context, login):
                continue
            if counts.get(guest, 0) >= DAILY_CAP:
                if our_status(statuses, context, login) is None:
                    actions.append(("wait", repo, sha, kind, tag))
                    if not dry_run:
                        org.post(f"/repos/{ORG}/{repo}/statuses/{sha}", {
                            "state": "pending", "context": context,
                            "description": f"{WAITING} {DAILY_CAP} checks ran today already. This one runs tomorrow."})
                continue
            counts[guest] = counts.get(guest, 0) + 1
            actions.append(("check", repo, sha, kind, tag))
            if dry_run:
                continue
            pipeline.post(f"/repos/{PIPELINE_REPO}/actions/workflows/check.yml/dispatches", {
                "ref": "main", "inputs": {"repo": repo, "sha": sha, "kind": kind, "tag": tag}})
            org.post(f"/repos/{ORG}/{repo}/statuses/{sha}", {
                "state": "pending", "context": context,
                "description": ("Queued: asking Andrew to approve the new tester list." if kind == "testers"
                                else "Queued: checking and building this commit."),
                "target_url": f"https://github.com/{PIPELINE_REPO}/actions/workflows/check.yml"})
    return actions


def write_repos_output(names: list[str]) -> None:
    """repos=a,b for the next step's app token (`repositories` input)."""
    for n in names:
        if not REPO_NAME_RE.fullmatch(n) or n in RESERVED_REPOS:
            raise SystemExit(f"refusing a non-guest repo name in the token scope: {n!r}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as fh:
            fh.write(f"repos={','.join(names)}\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="poll.py")
    ap.add_argument("--dry-run", action="store_true", help="list what would start; write nothing")
    ap.add_argument("--list-repos", action="store_true",
                    help="write the guest repo names to $GITHUB_OUTPUT as repos=a,b and exit")
    args = ap.parse_args(argv)
    org = Client.from_env(APP_TOKEN_ENV)
    if args.list_repos:
        names = sorted(guest_repos(org))
        write_repos_output(names)
        print(f"{len(names)} guest repo(s)")
        return 0
    pipeline = Client.from_env("GITHUB_TOKEN")
    actions = poll(org, pipeline, dt.datetime.now(dt.timezone.utc).date(), args.dry_run)
    started = sum(1 for a in actions if a[0] == "check")
    waiting = sum(1 for a in actions if a[0] == "wait")
    testers = sum(1 for a in actions if a[0] == "check" and a[3] == "testers")
    # Counts only: repo names and SHAs are not secret, but nothing guest-written is printed.
    print(f"{'would start' if args.dry_run else 'started'} {started} check(s), {testers} of them tester-list "
          f"requests; {waiting} waiting on the daily cap")
    return 0


if __name__ == "__main__":
    sys.exit(main())
