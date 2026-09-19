"""Gate job: resolve the app, verify the commit, download the guest repo as data.

    APP_TOKEN=... REPO=sam-hello SHA=<40 hex> KIND=push|tag TAG=v1.2 \\
        python3 ci/fetch_source.py OUT_TARBALL OUT_TREE_JSON

No git runs on guest content: the tree comes from GitHub's tarball endpoint for
that exact SHA, so no hook, filter, LFS smudge or `.git/config` of the guest's
can do anything. Writes `bundle_id` and `tree_sha` to $GITHUB_OUTPUT.

The tarball is `git archive` output, which applies the repo's own
`.gitattributes` (export-subst, export-ignore). So the commit's tree listing is
saved beside it (OUT_TREE_JSON, never printed: it names guest files), and
`ci/unpack.py --tree` refuses an archive that is not exactly that tree.

The bundle ID comes from the repo's `bundle_id` custom property, which only an
org owner can set (a guest collaborator cannot), never from the guest's files.
Prints nothing guest-written: the log of this public repo is public.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ghapi import APP_TOKEN_ENV, ORG, Client, GitHubError, validate_inputs  # noqa: E402

MAX_TARBALL_BYTES = 200 * 1024 * 1024
BUNDLE_ID_RE = re.compile(r"com\.willoughbytools\.[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+")


def app_properties(client: Client, repo: str) -> dict:
    """The org-owned custom property values for one repo ({} when it has none)."""
    rows = client.get(f"/repos/{ORG}/{repo}/properties/values") or []
    return {p["property_name"]: p["value"] for p in rows}


def resolve_bundle_id(props: dict) -> str:
    bundle_id = props.get("bundle_id") or ""
    if not BUNDLE_ID_RE.fullmatch(bundle_id):
        raise SystemExit("this repo has no valid bundle_id custom property: onboarding sets it")
    return bundle_id


def verify_commit(client: Client, repo: str, sha: str, kind: str, tag: str) -> str:
    """The commit's tree SHA, after checking the commit is in this repo and a tag names it."""
    commit = client.get(f"/repos/{ORG}/{repo}/git/commits/{sha}")
    if commit.get("sha") != sha:
        raise SystemExit("the commit does not resolve to itself")
    if kind == "tag":
        ref = client.get(f"/repos/{ORG}/{repo}/git/ref/tags/{tag}")
        obj = ref["object"]
        for _ in range(3):  # an annotated tag points at a tag object first
            if obj["type"] == "commit":
                break
            obj = client.get(f"/repos/{ORG}/{repo}/git/tags/{obj['sha']}")["object"]
        if obj["type"] != "commit" or obj["sha"] != sha:
            raise SystemExit("the tag no longer points at this commit")
    return commit["tree"]["sha"]


def tree_listing(client: Client, repo: str, tree_sha: str) -> dict:
    """The commit's whole tree, recursively ({"tree": [...], "truncated": bool})."""
    listing = client.get(f"/repos/{ORG}/{repo}/git/trees/{tree_sha}?recursive=1")
    if not isinstance(listing, dict) or listing.get("sha") != tree_sha:
        raise SystemExit("the tree listing is not the commit's tree")
    return {"sha": tree_sha, "truncated": listing.get("truncated"), "tree": [
        {k: e.get(k) for k in ("path", "mode", "type", "sha")} for e in listing.get("tree") or []]}


def write_outputs(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as fh:
        for k, v in values.items():
            if "\n" in v:
                raise ValueError(f"output {k} has a newline")
            fh.write(f"{k}={v}\n")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    repo, sha = os.environ.get("REPO", ""), os.environ.get("SHA", "")
    kind, tag = os.environ.get("KIND", ""), os.environ.get("TAG", "")
    validate_inputs(repo, sha, kind, tag)
    client = Client.from_env(APP_TOKEN_ENV)
    bundle_id = resolve_bundle_id(app_properties(client, repo))
    for out in argv:  # the handoff folder does not exist on a fresh runner (first live run, 2026-09-18)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
    try:
        tree_sha = verify_commit(client, repo, sha, kind, tag)
        size = client.download(f"/repos/{ORG}/{repo}/tarball/{sha}", argv[0], MAX_TARBALL_BYTES)
        Path(argv[1]).write_text(json.dumps(tree_listing(client, repo, tree_sha)))
    except GitHubError as e:
        raise SystemExit(f"GitHub refused: HTTP {e.status}") from None
    write_outputs(bundle_id=bundle_id, tree_sha=tree_sha)
    print(f"fetched {size} bytes for the check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
