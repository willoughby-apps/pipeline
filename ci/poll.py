"""Poll: start check.yml for every new branch head and v* tag in the guest repos.

    ORG_TOKEN=... GITHUB_TOKEN=... python3 ci/poll.py [--dry-run]

A guest repo is any org repo with a `bundle_id` custom property (onboarding
sets it with `guest`; only an org owner can). For each branch head and each
`v<major>[.<minor>[.<patch>]]` tag, a commit that carries no status of ours in
that kind's context gets one check: this dispatches check.yml (a
workflow_dispatch from GITHUB_TOKEN does start a run, per GitHub's docs) and
immediately marks the commit `pending`, which is what stops the next poll from
dispatching it again.

"Ours" means created by the ORG_TOKEN's own user: a guest can write statuses on
their repo, but not as that user.

Per guest, at most DAILY_CAP checks start per UTC day, counted from check.yml's
own runs (their run-name carries the repo). Past the cap the commit gets a
`pending` "Waiting" status and is picked up the next day.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ghapi import ORG, PIPELINE_REPO, REPO_NAME_RE, SHA_RE, TAG_RE, Client  # noqa: E402

DAILY_CAP = 20
CONTEXTS = {"push": "willoughby/check", "tag": "willoughby/release"}
WAITING = "Waiting:"
MAX_BRANCHES = 30
MAX_TAGS = 30


def guest_repos(org: Client) -> dict[str, str]:
    """{repo: guest} for every repo onboarding labelled."""
    out = {}
    for row in org.paginate(f"/orgs/{ORG}/properties/values"):
        props = {p["property_name"]: p["value"] for p in row.get("properties", [])}
        name = row.get("repository_name", "")
        if props.get("bundle_id") and REPO_NAME_RE.fullmatch(name):
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


def poll(org: Client, pipeline: Client, today: dt.date, dry_run: bool = False) -> list[tuple]:
    login = org.get("/user")["login"]
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
    return actions


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="poll.py")
    ap.add_argument("--dry-run", action="store_true", help="list what would start; write nothing")
    args = ap.parse_args(argv)
    org = Client.from_env("ORG_TOKEN")
    pipeline = Client.from_env("GITHUB_TOKEN")
    actions = poll(org, pipeline, dt.datetime.now(dt.timezone.utc).date(), args.dry_run)
    started = sum(1 for a in actions if a[0] == "check")
    waiting = sum(1 for a in actions if a[0] == "wait")
    # Counts only: repo names and SHAs are not secret, but nothing guest-written is printed.
    print(f"{'would start' if args.dry_run else 'started'} {started} check(s); {waiting} waiting on the daily cap")
    return 0


if __name__ == "__main__":
    sys.exit(main())
