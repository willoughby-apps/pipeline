"""Unpack a guest repo tarball (GitHub's `/tarball/{sha}`) as data.

    python3 ci/unpack.py ARCHIVE DEST [--tree TREE_JSON]

GitHub's tarball has one top-level folder (`owner-repo-shortsha/`); its
contents land directly in DEST. Python's `data` extraction filter refuses
absolute paths, `..`, links that leave DEST, devices and FIFOs, and drops
set-uid bits. A symlink that stays inside is kept so the gate can see it and
block it (the gate, not the unpacker, owns that rule). Anything refused makes
the whole unpack fail: the repo is then reported as blocked, never partially
checked. Exit 0 = unpacked, 3 = refused.

With `--tree` (the commit's tree as GitHub's `GET .../git/trees/{tree}?recursive=1`
returns it, written by ci/fetch_source.py), every file in the archive must be
exactly a blob of that tree, byte for byte (its git blob SHA-1), and every blob
of the tree must be in the archive. GitHub's tarball is `git archive` output,
which applies `.gitattributes` export-subst (a `$Format:%B$` comment becomes
the commit message, so arbitrary code), export-ignore (a file people and the
review see is never built), ident and working-tree-encoding. The gate forbids
those attributes by name; this makes "what is built is the tree people read" a
fact rather than a parse of `.gitattributes`.
"""
from __future__ import annotations

import hashlib
import io
import json
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


def git_blob_sha(fileobj, size: int) -> str:
    """The git object id of a blob with these bytes (SHA-1 of `blob <size>\\0` + bytes)."""
    h = hashlib.sha1(b"blob %d\0" % size)
    read = 0
    while True:
        chunk = fileobj.read(1 << 20)
        if not chunk:
            break
        read += len(chunk)
        h.update(chunk)
    if read != size:
        raise UnpackError("an archive member is shorter than its header says")
    return h.hexdigest()


def verify_tree(tf: tarfile.TarFile, kept: list[tarfile.TarInfo], tree: dict) -> None:
    """Refuse unless the archive's files are exactly the tree's blobs."""
    if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
        raise UnpackError("the commit's file list could not be read")
    if tree.get("truncated") is not False:
        raise UnpackError("the commit's file list is too long to compare")
    want = {}
    for e in tree["tree"]:
        kind, path = e.get("type"), e.get("path")
        if kind == "tree":
            continue
        if kind == "commit":
            raise UnpackError(f"{path} is a submodule")
        if kind != "blob" or not isinstance(path, str):
            raise UnpackError("the commit's file list has an entry that is not a file")
        want[path] = (e.get("mode") == "120000", e.get("sha"))
    got = {}
    for m in kept:
        if m.isdir():
            continue
        if m.issym():
            target = m.linkname.encode("utf-8", "surrogateescape")
            got[m.name] = (True, git_blob_sha(io.BytesIO(target), len(target)))
        elif m.isfile():
            got[m.name] = (False, git_blob_sha(tf.extractfile(m), m.size))
        else:
            raise UnpackError(f"{m.name} is not a file git archive writes")
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    changed = sorted(p for p in set(want) & set(got) if want[p] != got[p])
    problems = []
    if missing:
        problems.append("in the commit but not in the packaged code (export-ignore?): " + ", ".join(missing[:10]))
    if extra:
        problems.append("in the packaged code but not in the commit: " + ", ".join(extra[:10]))
    if changed:
        problems.append("different from the commit when packaged (export-subst, ident or an encoding?): "
                        + ", ".join(changed[:10]))
    if problems:
        raise UnpackError("the code packaged for the build is not the commit's code; " + "; ".join(problems))


def unpack(archive: str | Path, dest: str | Path, tree: dict | None = None) -> int:
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
            if tree is not None:
                verify_tree(tf, kept, tree)
            tf.extractall(dest, members=kept, filter="data")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise UnpackError(f"{type(e).__name__}: {e}") from None
    return len(kept)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    tree = None
    if len(argv) == 4 and argv[2] == "--tree":
        try:
            tree = json.loads(Path(argv[3]).read_text())
        except (OSError, ValueError) as e:
            print(f"refused: the commit's file list could not be read ({type(e).__name__})", file=sys.stderr)
            return 3
        argv = argv[:2]
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        n = unpack(argv[0], argv[1], tree)
    except UnpackError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 3
    print(f"unpacked {n} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
