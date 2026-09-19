"""`testers.txt`, read as data: the one parser for the format (PLAN section 1,
2026-09-19 decision b). The pipeline's change summary (`gate/changes.py`),
the Mini's tester sync (`testers/`) and the reconciler (`onboard/`) all read
the file through here, so the list Andrew approves is the list that is applied.

The format:

    # notes start with #
    mom@example.com
    dad@example.com Jeff Cohen          <- an optional name after the address
    # external:
    coworker@example.com                <- only below this line

- Every address above `# external:` is a **friend**: an internal tester, invited
  to Andrew's App Store Connect team with the Marketing role (the lowest role
  Apple allows for internal testing), seeing this one app only.
- Addresses below a `# external:` line (any case, spaces allowed around the
  colon) are **external testers**: the app's external TestFlight group, which
  needs Apple's beta review for a new version.
- An address in both sections is a friend (the first mention wins).
- Anything else on a line is ignored except an optional name after the address
  (letters, spaces, `.`, `'` and `-`, up to 60 characters), used for the team
  invitation, which Apple requires to carry a first and last name.
- The cap (25 friends per app unless Andrew raises it) is applied by the
  caller, first lines first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})+")
EXTERNAL_RE = re.compile(r"#\s*external\s*:\s*", re.IGNORECASE)
NAME_RE = re.compile(r"[^\W\d_][\w .'-]{0,59}", re.UNICODE)
MAX_EMAIL = 254


@dataclass(frozen=True)
class Person:
    email: str                 # lower case
    first: str = ""
    last: str = ""


@dataclass
class TestersFile:
    friends: list[Person] = field(default_factory=list)
    external: list[str] = field(default_factory=list)
    bad_lines: int = 0

    @property
    def friend_emails(self) -> list[str]:
        return [p.email for p in self.friends]


def parse(text: str | None) -> TestersFile:
    out = TestersFile()
    seen: set[str] = set()
    external = False
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("﻿").strip()
        if not line:
            continue
        if line.startswith("#"):
            if EXTERNAL_RE.fullmatch(line):
                external = True
            continue
        parts = line.split(None, 1)
        email = parts[0].lower()
        if not EMAIL_RE.fullmatch(email) or len(email) > MAX_EMAIL:
            out.bad_lines += 1
            continue
        if email in seen:
            continue
        seen.add(email)
        if external:
            out.external.append(email)
            continue
        first = last = ""
        if len(parts) > 1:
            name = " ".join(parts[1].split())
            if NAME_RE.fullmatch(name):
                words = name.split(" ")
                first, last = words[0], " ".join(words[1:])
        out.friends.append(Person(email, first, last))
    return out
