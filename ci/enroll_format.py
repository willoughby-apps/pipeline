"""The enroll issue: the one contract between the plugin's setup skill and enroll.

A new guest's Claude (`/willoughby-apps:setup <code>`, guest-apps/start/)
opens an issue in this public repo; the enroll job (phase C, part 2) redeems
the code, adds the issue's author to their guest repo and deletes the issue.
The setup skill writes exactly what `issue_title` and `issue_body` return
(tests/test_start_plugin.py holds it to that), so change both sides together.

    Title: Enroll <github-login>
    Body:  <!-- willoughby-enroll v1 -->
           code: ABCD-EFGH
           github: <github-login>

Only the code and the GitHub username, never an email (Andrew already has it).
The GitHub account that redeems a code is the issue's AUTHOR (`user.login`,
set by GitHub), never the body's `github:` line, which anyone can type: the
line is there for people reading the issue, and `parse` refuses an issue
whose line names someone else. Invite codes are 8 characters from ALPHABET
(no 0/O, 1/I/L, so they survive being read aloud or retyped), shown as
XXXX-XXXX; `normalize_code` forgives case, spaces and a missing hyphen.

Stdlib only: this file is published with the pipeline.
"""
from __future__ import annotations

import re

MARKER = "<!-- willoughby-enroll v1 -->"
TITLE_PREFIX = "Enroll "
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(rf"[{ALPHABET}]{{4}}-[{ALPHABET}]{{4}}")
# GitHub logins: 1 to 39 characters, alphanumerics and single hyphens, not at either end.
LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
MAX_BODY = 500


class EnrollFormatError(ValueError):
    pass


def normalize_code(text: str) -> str:
    """'abcd efgh', 'ABCDEFGH' or ' abcd-efgh ' -> 'ABCD-EFGH'; refuses anything else."""
    raw = re.sub(r"[\s-]", "", str(text)).upper()
    code = f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else raw
    if not CODE_RE.fullmatch(code):
        raise EnrollFormatError("not an invite code (8 letters and digits, like ABCD-EFGH)")
    return code


def issue_title(login: str) -> str:
    if not LOGIN_RE.fullmatch(login):
        raise EnrollFormatError(f"not a GitHub username: {login!r}")
    return TITLE_PREFIX + login


def issue_body(code: str, login: str) -> str:
    issue_title(login)
    return f"{MARKER}\ncode: {normalize_code(code)}\ngithub: {login}\n"


def parse(title: str, body: str, author: str) -> tuple[str, str]:
    """(code, login) from an enroll issue, or EnrollFormatError.

    `author` is the issue's `user.login` from GitHub. It is the login returned,
    and the title and body must both name it."""
    if not LOGIN_RE.fullmatch(author or ""):
        raise EnrollFormatError("the issue has no valid author")
    if len(body or "") > MAX_BODY:
        raise EnrollFormatError("the body is too long to be an enroll request")
    if (title or "").strip().lower() != issue_title(author).lower():
        raise EnrollFormatError("the title does not name the issue's author")
    lines = [ln.strip() for ln in (body or "").replace("\r\n", "\n").strip().split("\n") if ln.strip()]
    if len(lines) != 3 or lines[0] != MARKER:
        raise EnrollFormatError("the body is not an enroll request")
    fields = {}
    for ln in lines[1:]:
        key, sep, value = ln.partition(":")
        if not sep or key.strip().lower() not in ("code", "github") or key.strip().lower() in fields:
            raise EnrollFormatError("the body is not an enroll request")
        fields[key.strip().lower()] = value.strip()
    if fields["github"].lower() != author.lower():
        raise EnrollFormatError("the body names a different GitHub account from the one that opened the issue")
    return normalize_code(fields["code"]), author
