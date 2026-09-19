"""The enroll job (.github/workflows/enroll.yml): redeem an invite code.

A new guest's `/willoughby-apps:setup CODE` opens an issue here in the format
`enroll_format.py` defines. This repo is public, so the issue and this job's
log are public. Three steps, each its own command:

    redeem   (GITHUB_TOKEN, INVITE_CODES)  blank the issue first, check the
             author's request count, parse, look the code up; the one guest
             repo goes to a masked step output, never to the log.
    invite   (APP_TOKEN: the app, that one repo, administration write)
             refuse a spent code, invite the author with push permission,
             and undo the invitation if another one won a race.
    finish   (GITHUB_TOKEN)  blank the issue again, one neutral comment,
             close, lock. Runs whatever happened before it.

Untrusted text (title, body, author) arrives through env only and is never
printed. INVITE_CODES is a repo secret, JSON {sha256(code): {"repo", "guest"}},
re-set as a whole by `python3 -m onboard` on the Mini; the code itself is
never stored anywhere but Andrew's registry. A code is SPENT once its repo has
any direct collaborator or pending invitation, so it needs no state of its own
here. The comment never says whether a code was known: "received" is the same
words for a match, a spent code and an unknown one.

GitHub keeps an issue's edit history, readable by anyone who can read the
issue, so blanking the body hides the code from the page and from search but
not from that history. That is why a code is worth nothing once it has been
used, and why an unknown code shown there gives nothing away.

Stdlib only: this file is published with the pipeline.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import enroll_format  # noqa: E402
from ghapi import ORG, PIPELINE_REPO, REPO_NAME_RE, RESERVED_REPOS, APP_TOKEN_ENV, Client, GitHubError  # noqa: E402

REDACTED_TITLE = "Enroll request"
REDACTED_BODY = "(Removed. Enroll requests are read once and not kept here.)"
# Issues one account may open here in 24 hours before the rest go unchecked.
MAX_REQUESTS_PER_DAY = 3
PERMISSION = "push"  # GitHub's name for write access

RESULTS = ("received", "format", "busy")
MESSAGES = {
    "received": ("Thanks, this enroll request was read and removed from view. If the invite code "
                 "was valid and unused, an invitation to your app is waiting for this GitHub account "
                 "(it also arrives by email). If nothing arrives within 10 minutes, send Andrew your "
                 "GitHub username."),
    "format": ("This was not in the form of an enroll request, so nothing was done with it. "
               "Run /willoughby-apps:setup again with the code from Andrew's message."),
    "busy": ("This account has sent too many enroll requests today, so this one was not checked. "
             "Send Andrew your GitHub username instead."),
}


class EnrollError(RuntimeError):
    pass


# ------------------------------------------------------------------ codes


def code_hash(code: str) -> str:
    """SHA-256 hex of the normalized code (what INVITE_CODES is keyed by)."""
    return hashlib.sha256(enroll_format.normalize_code(code).encode()).hexdigest()


def valid_guest_repo(repo: str) -> bool:
    return bool(REPO_NAME_RE.fullmatch(repo or "")) and repo not in RESERVED_REPOS


def load_codes(text: str) -> dict:
    """INVITE_CODES as {hash: repo}. Refuses a malformed secret as a whole."""
    try:
        raw = json.loads(text or "{}")
    except ValueError:
        raise EnrollError("INVITE_CODES is not JSON") from None
    if not isinstance(raw, dict):
        raise EnrollError("INVITE_CODES is not a JSON object")
    out = {}
    for h, entry in raw.items():
        repo = entry.get("repo") if isinstance(entry, dict) else None
        if not (isinstance(h, str) and len(h) == 64 and all(c in "0123456789abcdef" for c in h)):
            raise EnrollError("INVITE_CODES has a key that is not a SHA-256")
        if not valid_guest_repo(repo):
            raise EnrollError("INVITE_CODES names something that is not a guest repo")
        out[h] = repo
    return out


def lookup(codes: dict, code: str) -> str | None:
    return codes.get(code_hash(code))


# ----------------------------------------------------------- GitHub steps


def issue_path(number) -> str:
    n = str(number)
    if not n.isdigit():
        raise EnrollError("ISSUE_NUMBER is not a number")
    return f"/repos/{PIPELINE_REPO}/issues/{n}"


def redact(client, number) -> None:
    client.json("PATCH", issue_path(number), {"title": REDACTED_TITLE, "body": REDACTED_BODY})


def recent_requests(client, login: str, now: dt.datetime) -> int:
    """Issues (not pull requests) `login` opened here in the last 24 hours, this one included."""
    since = now - dt.timedelta(hours=24)
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    items = client.paginate(f"/repos/{PIPELINE_REPO}/issues?state=all&creator={login}&since={stamp}", limit=100)
    count = 0
    for it in items:
        if "pull_request" in it:
            continue
        created = dt.datetime.strptime(it["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        if created >= since:
            count += 1
    return count


def redeem(client, env: dict, now: dt.datetime) -> tuple[str, str | None]:
    """(result, repo or None). Blanks the issue before reading anything else."""
    redact(client, env.get("ISSUE_NUMBER", ""))
    author = env.get("ISSUE_AUTHOR", "")
    try:
        code, login = enroll_format.parse(env.get("ISSUE_TITLE", ""), env.get("ISSUE_BODY", ""), author)
    except enroll_format.EnrollFormatError:
        return "format", None
    if recent_requests(client, login, now) > MAX_REQUESTS_PER_DAY:
        return "busy", None
    return "received", lookup(load_codes(env.get("INVITE_CODES", "")), code)


def is_spent(app, repo: str) -> bool:
    if app.paginate(f"/repos/{ORG}/{repo}/collaborators?affiliation=direct", limit=1):
        return True
    return bool(app.paginate(f"/repos/{ORG}/{repo}/invitations", limit=1))


def invite(app, repo: str, login: str) -> str:
    """Invite `login` to `repo` unless the code is spent. Returns what happened
    (for tests; the job prints nothing that names the repo)."""
    if not valid_guest_repo(repo):
        raise EnrollError("not a guest repo")
    if not enroll_format.LOGIN_RE.fullmatch(login or ""):
        raise EnrollError("not a GitHub username")
    if is_spent(app, repo):
        return "spent"
    made = app.json("PUT", f"/repos/{ORG}/{repo}/collaborators/{login}", {"permission": PERMISSION})
    if not made:
        # 204: the account already has access (an org member), so no invitation was made.
        return "has-access"
    mine = made["id"]
    # Two requests with the same code can both pass is_spent before either
    # invites. The earliest invitation wins; every other one withdraws itself.
    others = [c["login"] for c in app.paginate(f"/repos/{ORG}/{repo}/collaborators?affiliation=direct")
              if c["login"].lower() != login.lower()]
    invitations = sorted(app.paginate(f"/repos/{ORG}/{repo}/invitations"), key=lambda i: i["id"])
    if others or (invitations and invitations[0]["id"] != mine):
        app.json("DELETE", f"/repos/{ORG}/{repo}/invitations/{mine}")
        return "lost-race"
    return "invited"


def finish(client, number, result: str) -> None:
    message = MESSAGES.get(result, MESSAGES["received"])
    path = issue_path(number)
    client.json("PATCH", path, {"title": REDACTED_TITLE, "body": REDACTED_BODY,
                                "state": "closed", "state_reason": "completed"})
    client.json("POST", f"{path}/comments", {"body": message})
    client.json("PUT", f"{path}/lock", {"lock_reason": "resolved"})


# -------------------------------------------------------------------- CLI


def write_outputs(result: str, repo: str | None) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if repo:
        # Masked before it can appear anywhere: the next step's inputs are logged.
        print(f"::add-mask::{repo}", flush=True)
    lines = f"result={result}\nrepo={repo or ''}\n"
    if out:
        with open(out, "a") as fh:
            fh.write(lines)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""
    env = dict(os.environ)
    if cmd == "redeem":
        result, repo = redeem(Client.from_env("GITHUB_TOKEN"), env, dt.datetime.now(dt.timezone.utc))
        write_outputs(result, repo)
        print("request read", flush=True)
        return 0
    if cmd == "invite":
        invite(Client.from_env(APP_TOKEN_ENV), env.get("GUEST_REPO", ""), env.get("ISSUE_AUTHOR", ""))
        print("done", flush=True)
        return 0
    if cmd == "finish":
        finish(Client.from_env("GITHUB_TOKEN"), env.get("ISSUE_NUMBER", ""), env.get("RESULT", ""))
        print("issue closed", flush=True)
        return 0
    print("usage: enroll.py redeem|invite|finish", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (EnrollError, GitHubError) as e:
        # Neither names the code, the body or the repo (GitHubError drops the query string).
        print(f"error: {type(e).__name__}", file=sys.stderr)
        sys.exit(1)
