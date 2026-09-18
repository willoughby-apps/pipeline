"""Unpack a guest repo tarball (GitHub's `/tarball/{sha}`) as data.

    python3 ci/unpack.py ARCHIVE DEST

GitHub's tarball has one top-level folder (`owner-repo-shortsha/`); its
contents land directly in DEST. Python's `data` extraction filter refuses
absolute paths, `..`, links that leave DEST, devices and FIFOs, and drops
set-uid bits. A symlink that stays inside is kept so the gate can see it and
block it (the gate, not the unpacker, owns that rule). Anything refused makes
the whole unpack fail: the repo is then reported as blocked, never partially
checked. Exit 0 = unpacked, 3 = refused.
"""
from __future__ import annotations

import sys
import tarfile
from pathlib import Path

MAX_MEMBERS = 20000
MAX_TOTAL_BYTES = 500 * 1024 * 1024


class UnpackError(ValueError):
    pass


def _strip_top(members: list[tarfile.TarInfo]) -> tuple[str, list[tarfile.TarInfo]]:
    tops = {m.name.split("/", 1)[0] for m in members if m.name not in ("", ".")}
    if len(tops) != 1:
        raise UnpackError(f"expected one top-level folder, found {len(tops)}")
    top = tops.pop()
    kept = []
    for m in members:
        if m.name == top:
            continue
        if not m.name.startswith(top + "/"):
            raise UnpackError("member outside the top-level folder")
        m = m.replace(name=m.name[len(top) + 1:], deep=False)
        if m.islnk():
            if not m.linkname.startswith(top + "/"):
                raise UnpackError("hard link outside the repo")
            m = m.replace(linkname=m.linkname[len(top) + 1:], deep=False)
        kept.append(m)
    return top, kept


def unpack(archive: str | Path, dest: str | Path) -> int:
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise UnpackError(f"{dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, mode="r:gz") as tf:
            members = tf.getmembers()
            if len(members) > MAX_MEMBERS:
                raise UnpackError(f"more than {MAX_MEMBERS} entries")
            if sum(m.size for m in members if m.isfile()) > MAX_TOTAL_BYTES:
                raise UnpackError("unpacked size is over the cap")
            _, kept = _strip_top(members)
            tf.extractall(dest, members=kept, filter="data")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise UnpackError(f"{type(e).__name__}: {e}") from None
    return len(kept)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        n = unpack(argv[0], argv[1])
    except UnpackError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 3
    print(f"unpacked {n} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
