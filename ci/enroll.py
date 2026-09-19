"""The enroll job (.github/workflows/enroll.yml): redeem an invite code.

A new guest's `/willoughby-apps:setup CODE` opens an issue here in the format
`enroll_format.py` defines. This repo is public, so the issue and this job's
log are public. Three steps, each its own command:

    redeem   (GITHUB_TOKEN, INVITE_CODES)  blank the issue first, check the
             author's request count, parse, refuse a code presented before,
             look the code up, and post the one comment, which records this
             code as presented; the one guest repo goes to a masked step
             output, never to the log.
    invite   (APP_TOKEN: the app, that one repo, administration write)
             refuse a spent code, invite the author with push permission,
             and undo the invitation if another one won a race.
    finish   (GITHUB_TOKEN)  blank the issue again, the comment if redeem
             did not get that far, close, lock. Runs whatever happened before it.

Untrusted text (number, title, body, author) is read from the event file
($GITHUB_EVENT_PATH) and never printed. Never from env: a step's env values
are printed in its public log header, which put a code in the log on the
first live run (2026-09-19). INVITE_CODES is a repo secret, JSON {sha256(code): {"repo", "guest"}},
re-set as a whole by `python3 -m onboard` on the Mini; the code itself is
never stored anywhere but Andrew's registry.

A code works ONCE. GitHub keeps an issue's edit history, readable by anyone,
so a code stays public after the body is blanked, and "spent" must hold for
good. The repo's collaborators and invitations do not: a guest who declines
the invitation, an invitation withdrawn, or a collaborator removed would put
the code back in play for whoever read it from that history (review,
2026-09-19). So every code this job checks is recorded as PRESENTED, in the
job's own comment (`request_mark`, an HTML comment inside it, written by
github-actions[bot], which nothing here ever edits or deletes), and a code
presented once is refused for good, as is one whose repo has any direct
collaborator or pending invitation. Every checked code is recorded, known or
not, so the record says nothing about which codes were real. A request that
fails the format or the rate limit is not checked, so its code is not
recorded.

**The first account keeps its code (audit 2026-09-19).** The record is posted
before the invite step, so a failure after it (the app token, GitHub
refusing the invitation, a runner problem) used to spend the code with no
invitation made, and only Andrew could recover (`onboard code` and a new
welcome message). So the record also carries `owner_mark`: a digest of the
code with the numeric id (never the login, which can be renamed and taken)
of the account that presented it FIRST. A later request with the same code
from that same account is checked again; any other account is refused for
good, as before. The invite step still refuses a repo that has a direct
collaborator or a pending invitation, so a second try only ever invites when
the first one left nothing behind (or the guest declined it themselves).
`onboard code` is left for a code that no account of the guest's can use.

The comment never says whether a code was known: "received" is the same
words for a match, a spent code and an unknown one. The run page does show
it: the app-token and invite steps run only for a match, and step
conclusions are public. That tells a reader only that a code they already
saw in an edit history was real, and once checked that code is spent.

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
# The only author whose comments here count as this job's record: the app is
# not installed on this repo, so the job comments with its GITHUB_TOKEN.
ACTIONS_BOT = "github-actions[bot]"
MARK_PREFIX = "<!-- willoughby-enroll-request "
OWNER_PREFIX = "<!-- willoughby-enroll-owner "

RESULTS = ("received", "format", "busy")
MESSAGES = {
    "received": ("Thanks, this enroll request was read and removed from view. If the invite code "
                 "was valid and unused, an invitation to your app is waiting for this GitHub account "
                 "(it also arrives by email). If nothing arrives within 10 minutes, send the same "
                 "request once more from this same account: a code stays with the account that "
                 "used it first. If it still does not arrive, send Andrew your GitHub username."),
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


def request_mark(code: str) -> str:
    """What the job's comment records about a checked code. Domain-separated
    from the INVITE_CODES key, though it protects nothing the edit history
    does not already show."""
    digest = hashlib.sha256(b"willoughby-enroll-request\n" + enroll_format.normalize_code(code).encode()).hexdigest()
    return f"{MARK_PREFIX}{digest} -->"


def owner_mark(code: str, account_id: str) -> str | None:
    """The record of which account presented a code (GitHub's numeric user id,
    which a rename does not move). None without a numeric id."""
    if not (account_id or "").isdigit():
        return None
    digest = hashlib.sha256(b"willoughby-enroll-owner\n" + enroll_format.normalize_code(code).encode()
                            + b"\n" + account_id.encode()).hexdigest()
    return f"{OWNER_PREFIX}{digest} -->"


def first_record(client, mark: str) -> str | None:
    """The body of the job's own EARLIEST comment recording this code, or None
    (the API lists a repo's issue comments oldest first)."""
    for c in client.paginate(f"/repos/{PIPELINE_REPO}/issues/comments"):
        if (c.get("user") or {}).get("login") == ACTIONS_BOT and mark in (c.get("body") or ""):
            return c.get("body") or ""
    return None


def presented_before(client, mark: str) -> bool:
    """Whether any earlier request here carried this code (the job's own comments only)."""
    return first_record(client, mark) is not None


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


def issue_fields(event_path: str) -> dict:
    """The fields redeem needs, from the `issues` event payload GitHub writes to disk."""
    issue = json.loads(Path(event_path).read_text()).get("issue") or {}
    return {"ISSUE_NUMBER": str(issue.get("number", "")), "ISSUE_TITLE": issue.get("title") or "",
            "ISSUE_BODY": issue.get("body") or "", "ISSUE_AUTHOR": (issue.get("user") or {}).get("login") or "",
            "ISSUE_AUTHOR_ID": str((issue.get("user") or {}).get("id") or "")}


def comment_body(result: str, mark: str | None = None) -> str:
    message = MESSAGES.get(result, MESSAGES["received"])
    return f"{message}\n\n{mark}" if mark else message


def comment(client, number, result: str, mark: str | None = None) -> None:
    client.json("POST", f"{issue_path(number)}/comments", {"body": comment_body(result, mark)})


def redeem(client, env: dict, now: dt.datetime) -> tuple[str, str | None]:
    """(result, repo or None). Blanks the issue before reading anything else,
    and ends by posting the issue's one comment, which records a checked code."""
    number = env.get("ISSUE_NUMBER", "")
    redact(client, number)
    author = env.get("ISSUE_AUTHOR", "")
    try:
        code, login = enroll_format.parse(env.get("ISSUE_TITLE", ""), env.get("ISSUE_BODY", ""), author)
    except enroll_format.EnrollFormatError:
        comment(client, number, "format")
        return "format", None
    if recent_requests(client, login, now) > MAX_REQUESTS_PER_DAY:
        comment(client, number, "busy")
        return "busy", None
    codes = load_codes(env.get("INVITE_CODES", ""))
    mark = request_mark(code)
    owner = owner_mark(code, env.get("ISSUE_AUTHOR_ID", ""))
    first = first_record(client, mark)
    # A code presented before is spent, except for the account that presented
    # it first (a failed invite must not cost the guest their code).
    repo = lookup(codes, code) if first is None or (owner and owner in first) else None
    # Posted before the invite step runs, so the code is spent for every other
    # account before any invitation exists; a later request finds this comment.
    comment(client, number, "received", mark + (f"\n{owner}" if owner else ""))
    return "received", repo


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


def finish(client, number, result: str, commented: bool = False) -> None:
    path = issue_path(number)
    client.json("PATCH", path, {"title": REDACTED_TITLE, "body": REDACTED_BODY,
                                "state": "closed", "state_reason": "completed"})
    if not commented:
        comment(client, number, result)
    client.json("PUT", f"{path}/lock", {"lock_reason": "resolved"})


# -------------------------------------------------------------------- CLI


def write_outputs(result: str, repo: str | None) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if repo:
        # Masked before it can appear anywhere: the next step's inputs are logged.
        print(f"::add-mask::{repo}", flush=True)
    lines = f"result={result}\nrepo={repo or ''}\ncommented=yes\n"
    if out:
        with open(out, "a") as fh:
            fh.write(lines)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""
    env = dict(os.environ)
    if cmd == "redeem":
        env.update(issue_fields(env.get("GITHUB_EVENT_PATH", "")))
        result, repo = redeem(Client.from_env("GITHUB_TOKEN"), env, dt.datetime.now(dt.timezone.utc))
        write_outputs(result, repo)
        print("request read", flush=True)
        return 0
    if cmd == "invite":
        invite(Client.from_env(APP_TOKEN_ENV), env.get("GUEST_REPO", ""), env.get("ISSUE_AUTHOR", ""))
        print("done", flush=True)
        return 0
    if cmd == "finish":
        finish(Client.from_env("GITHUB_TOKEN"), env.get("ISSUE_NUMBER", ""), env.get("RESULT", ""),
               commented=env.get("COMMENTED", "") == "yes")
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
