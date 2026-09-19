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

**Testers (PLAN section 8).** For each guest repo whose default-branch head H
passed its check (the bot's `willoughby/check` status is success), the commit
C that last changed `testers.txt` as of H (`GET .../commits?sha=H&path=testers.txt`)
is looked up; when C carries no `willoughby/testers` status of ours,
`guest-apple.yml` is dispatched on ajcohen9/willoughby with (repo, H, C) and C
is marked `pending` there, which is what stops the next poll from dispatching
it again. A `pending` of ours older than TESTERS_STALE is dispatched again: the
Mini's run replaces it with `success` or `error`, so one still pending that
long was never run or never finished (a cancelled queue entry, a timeout
before the report, a Mini that was off). A sync is idempotent, so a second
one is harmless. The Mini reads the file at H as data and syncs the app's external
TestFlight group to it. This needs MONOREPO_DISPATCH (Actions write on the
monorepo); without it the testers step is skipped. It never counts towards
the daily cap: nothing is built.
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
CONTEXTS = {"push": "willoughby/check", "tag": "willoughby/release"}
TESTERS_CONTEXT = "willoughby/testers"
TESTERS_FILE = "testers.txt"
MONOREPO = "ajcohen9/willoughby"
APPLE_WORKFLOW = "guest-apple.yml"
WAITING = "Waiting:"
# guest-apple.yml's job has timeout-minutes 60 and queues (`queue: max`) behind
# at most a build hand-off of the same repo, itself up to 60: three hours is
# past anything that is still coming.
TESTERS_STALE = dt.timedelta(hours=3)
MAX_BRANCHES = 30
MAX_TAGS = 30


def guest_repos(org: Client) -> dict[str, str]:
    """{repo: guest} for every repo onboarding labelled."""
    out = {}
    for row in org.paginate(f"/orgs/{ORG}/properties/values"):
        props = {p["property_name"]: p["value"] for p in row.get("properties", [])}
        name = row.get("repository_name", "")
        if props.get("bundle_id") and REPO_NAME_RE.fullmatch(name) and name not in RESERVED_REPOS:
            out[name] = props.get("guest") or name.split("-", 1)[0]
    return out


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


def stale_pending(status: dict, now: dt.datetime) -> bool:
    """A `pending` of ours that no run has replaced within TESTERS_STALE."""
    if status.get("state") != "pending":
        return False
    try:
        at = dt.datetime.fromisoformat((status.get("updated_at") or status.get("created_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    if at.tzinfo is None:
        return False
    return now - at > TESTERS_STALE


def testers_change(org: Client, repo: str, login: str, now: dt.datetime | None = None) -> tuple[str, str] | None:
    """(H, C) when the default branch head H passed its check and C, the commit
    that last changed testers.txt as of H, has no testers status of ours yet, or
    only a `pending` one gone stale."""
    default = (org.get(f"/repos/{ORG}/{repo}") or {}).get("default_branch") or ""
    if not default:
        return None
    head = ((org.get(f"/repos/{ORG}/{repo}/branches/{default}") or {}).get("commit") or {}).get("sha") or ""
    if not SHA_RE.fullmatch(head):
        return None
    checked = our_status(org.paginate(f"/repos/{ORG}/{repo}/commits/{head}/statuses", limit=100),
                         CONTEXTS["push"], login)
    if not checked or checked.get("state") != "success":
        return None
    changes = org.get(f"/repos/{ORG}/{repo}/commits?sha={head}&path={TESTERS_FILE}&per_page=1") or []
    change = (changes[0] or {}).get("sha", "") if changes else ""
    if not SHA_RE.fullmatch(change):
        return None
    ours = our_status(org.paginate(f"/repos/{ORG}/{repo}/commits/{change}/statuses", limit=100),
                      TESTERS_CONTEXT, login)
    if ours and not stale_pending(ours, now or dt.datetime.now(dt.timezone.utc)):
        return None
    return head, change


def poll_testers(org: Client, mono: Client | None, repos: dict[str, str], dry_run: bool,
                 now: dt.datetime | None = None) -> list[tuple]:
    login = BOT_LOGIN
    actions = []
    for repo in sorted(repos):
        try:
            found = testers_change(org, repo, login, now)
        except GitHubError as e:
            # One repo's lookup failing (say, an empty repo) never stops the checks.
            print(f"testers: {repo} skipped (HTTP {e.status})")
            continue
        if not found:
            continue
        head, change = found
        actions.append(("testers", repo, head, "testers", change))
        if dry_run or mono is None:
            continue
        try:
            mono.post(f"/repos/{MONOREPO}/actions/workflows/{APPLE_WORKFLOW}/dispatches", {
                "ref": "main", "inputs": {"repo": repo, "sha": head, "change": change}})
        except GitHubError as e:
            # Not marked pending, so the next poll tries again.
            print(f"testers: could not start {APPLE_WORKFLOW} for {repo} (HTTP {e.status})")
            continue
        org.post(f"/repos/{ORG}/{repo}/statuses/{change}", {
            "state": "pending", "context": TESTERS_CONTEXT,
            "description": "Queued: updating the TestFlight testers from testers.txt."})
    return actions


def poll(org: Client, pipeline: Client, today: dt.date, dry_run: bool = False,
         mono: Client | None = None, now: dt.datetime | None = None) -> list[tuple]:
    login = BOT_LOGIN  # an installation token cannot call GET /user
    repos = guest_repos(org)
    counts = checks_today(pipeline, repos, today)
    actions = []
    for repo in sorted(repos):
        guest = repos[repo]
        for sha, kind, tag in candidates(org, repo):
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
                "description": "Queued: checking and building this commit.",
                "target_url": f"https://github.com/{PIPELINE_REPO}/actions/workflows/check.yml"})
    return actions + poll_testers(org, mono, repos, dry_run, now)


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
    mono = Client(os.environ["MONOREPO_DISPATCH"]) if os.environ.get("MONOREPO_DISPATCH") else None
    actions = poll(org, pipeline, dt.datetime.now(dt.timezone.utc).date(), args.dry_run, mono)
    started = sum(1 for a in actions if a[0] == "check")
    waiting = sum(1 for a in actions if a[0] == "wait")
    testers = sum(1 for a in actions if a[0] == "testers")
    # Counts only: repo names and SHAs are not secret, but nothing guest-written is printed.
    print(f"{'would start' if args.dry_run else 'started'} {started} check(s); {waiting} waiting on the daily cap")
    if testers and mono is None and not args.dry_run:
        print(f"{testers} tester list(s) changed, not synced: MONOREPO_DISPATCH is not set")
    else:
        print(f"{'would sync' if args.dry_run else 'syncing'} {testers} tester list(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
