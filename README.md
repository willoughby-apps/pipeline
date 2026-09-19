# willoughby-apps pipeline

Checks and builds the iPhone apps people make in the `willoughby-apps`
organization. This repo is public so its GitHub-hosted macOS minutes are free;
the guest repos it reads are private, so nothing here prints or uploads their
content unencrypted.

This repo is generated. Its source is `guest-apps/pipeline/` in Andrew's
monorepo (with `gate/` and `policy/` copied in from `guest-apps/`), published by
`guest-apps/scripts/publish_pipeline.sh`. Edit it there, not here.

## Workflows

| Workflow | When | What |
|---|---|---|
| `poll.yml` | every 5 minutes, or by hand (`dry_run`) | For each guest repo (an org repo with a `bundle_id` custom property), each branch head and `v*` tag without a status from us starts `check.yml` and is marked `pending`. At most 20 checks per guest per UTC day. |
| `check.yml` | dispatched by `poll.yml`, or by hand, or `workflow_call` | `fetch` → `gate` → `build` → `preview` → `report` → `request` (tags only). |

`check.yml`'s jobs:

1. **fetch** (Ubuntu). The only job before `report` that holds `ORG_TOKEN`,
   and it installs nothing but the SHA-256-pinned `age` and parses nothing a
   guest wrote. Resolves the bundle ID from the repo's `bundle_id` custom
   property (only an org owner can set it), checks the commit is in that repo
   (and, for a tag, that the tag still points at it), downloads GitHub's
   tarball of that SHA and the commit's tree listing, encrypts both to a
   one-run key and exits.
2. **gate** (Ubuntu). No secret. Installs the four scanners at pinned versions
   (semgrep and its whole dependency tree from the hash-locked
   `ci/requirements-gate.txt`, `--require-hashes --only-binary=:all:`), decrypts
   the source, checks its SHA-256, unpacks it as data (`ci/unpack.py --tree`:
   the archive must be exactly the commit's tree) and runs `python3 -m gate`.
   No git runs on guest content and no guest code runs.
3. **build** (macOS 26, Xcode 26.6). `permissions: {}`, no secret. `xcodegen`,
   then `xcodebuild archive` for a device and `xcodebuild build` for the
   simulator, both `CODE_SIGNING_ALLOWED=NO`. Output goes to `build.log`, never
   the job log. It records the SHA-256 of `unsigned.ipa` as a job output.
4. **preview** (macOS 26). `permissions: {}`, no secret. Installs the simulator
   build, launches it and takes a screenshot. The only job where guest code
   runs.
5. **report** (Ubuntu). Decrypts the reports with `PIPELINE_PRIVATE_KEY` and
   posts, on the guest commit: a status (`willoughby/check` for a push,
   `willoughby/release` for a tag), a comment with the gate failures or compile
   errors, and the screenshot, which it commits under the non-branch ref
   `refs/willoughby/previews` in the guest repo. Deletes the one-run hand-off
   artifacts.
6. **request** (Ubuntu, tags that passed). Opens `Release request: vX` in the
   guest repo and dispatches `guest-review.yml` on the monorepo.

## Secrets

| Secret | Used by | Scope |
|---|---|---|
| `ORG_TOKEN` | `poll`, `fetch`, `report`, `request` | fine-grained PAT on `willoughby-apps`, **repository access limited to the guest repos** (never this repo, `app-template` or `start`): Contents read and write (the tarball; the previews ref), Commit statuses write, Issues write, Metadata read; organization Custom properties read. No Administration. |
| `MONOREPO_DISPATCH` | `request` | fine-grained PAT on `ajcohen9/willoughby`: Actions write. |
| `PIPELINE_PRIVATE_KEY` | `report` | the age identity for `keys/pipeline.age.pub` (`guest-apps/scripts/pipeline_keypair.sh`). The Mini holds the same key to decrypt the unsigned build. |

`gate`, `build` and `preview` reference none of them; `build` and `preview`
also have a token with no permissions (`permissions: {}`). A secret referenced
anywhere in a job is handed to that runner when the job starts, and a job with
passwordless sudo cannot keep it from anything else running there. So the job
that installs third-party packages and parses guest files (`gate`) holds none,
and the jobs that hold one install only `age`, pinned by SHA-256.

## Why it is built this way

**Everything that leaves a job is encrypted to `keys/pipeline.age.pub`.** A
public repo's logs and artifacts are readable by anyone. `age` over OpenSSL:
age is authenticated encryption to an X25519 recipient in one command with no
choices to get wrong, where OpenSSL has no public-key file encryption short of
a hand-built hybrid (`enc` is unauthenticated CBC plus a separately wrapped
key). The release binary is pinned by SHA-256 (`ci/install_tools.sh`).

**Source hand-off without a long-lived secret in the build job.** The build
job must get the private source but may hold no secret, since it compiles
guest code. The fetch job (which holds `ORG_TOKEN`) encrypts the
tarball and the tree listing to a key pair it makes for this run only, uploads
the ciphertext with 1-day retention, and passes the one-run private key to
`gate` and `build` as a job output; `report` deletes the artifact at the end of
the run. Alternatives
rejected: a token in the build job (a fine-grained PAT reads every guest repo,
so guest code could read other guests' apps; GitHub-scoped per-repo tokens need
a GitHub App); `actions/checkout` with `ORG_TOKEN` (a secret in a guest-code
job); an unencrypted artifact (public). GitHub's docs advise against passing a
*masked* secret between jobs, since outputs holding one are dropped; this key
is not a registered secret, is never printed, opens only this run's ciphertext
of the source that `build` legitimately receives, and is useless after the
artifact is deleted. `gate` and `build` check the tarball's SHA-256 against
`fetch`'s before unpacking, so what is built is exactly what was gated.

**Compile and run on separate machines.** `preview` runs the guest's app, and
anything running on a runner can reach that run's artifact token. Keeping it
off the `build` machine means the unsigned build's SHA-256 was recorded by a
job that never ran guest code. `report` puts that digest in the commit status
(`unsigned sha256 <hex>`), and the Mini's `guest-sign.yml` must match the
decrypted `unsigned.ipa` against a status created by the pipeline's own user
(a guest can write statuses on their repo, but not as that user) before
signing.

**No caches.** Any job in a run can write the run's cache, so a guest-code job
could plant a poisoned entry (for example a ClamAV database) that a later
run's gate would restore. ClamAV signatures are downloaded fresh each run.

**What is built is the commit's tree, byte for byte.** GitHub's tarball is
`git archive` output, which applies the repo's own `.gitattributes`:
`export-subst` turns a `$Format:%B$` comment into the commit message (code the
file view, a clone and a git diff never show), and `export-ignore` drops a file
people can see. The gate forbids those attributes, and `ci/unpack.py --tree`
compares every file in the tarball with the commit's tree listing (git blob
SHA-1s, fetched by `ci/fetch_source.py`) and refuses any difference. The
advisory review reads the same bytes: `request.py` dispatches it with the
gate's `source_sha256`, and `review run --source-tarball` refuses a tarball
with any other digest.

**No untrusted text in a script.** No `run:` contains `${{ }}`; inputs go
through `env:` and `ci/ghapi.py validate_inputs` (repo name, 40-hex SHA,
`push`/`tag`, `vN[.N[.N]]`). Branch names, commit messages and issue text are
never used at all.

**Only Andrew can push to main.** Every run executes `ci/*.py` from main with
`PIPELINE_PRIVATE_KEY` and `MONOREPO_DISPATCH` in the environment, so a push to
main is as good as both secrets. A repository ruleset blocks creating,
updating, deleting and force-pushing the default branch for everyone but
Andrew's own user, and `ORG_TOKEN` does not have this repo in its repository
list at all. Adding a collaborator to a guest repo (enrolling) needs
Administration, which lives in a separate token for the future enroll job only.

**Actions.** GitHub-owned only (org policy), each pinned by full commit SHA.

**Runner images** (from `actions/runner-images`, 2026-09-18): `macos-26` is
macOS 26 arm64 with Xcode 26.6 as the default and the iOS 26.5 simulators;
`ubuntu-24.04` has Python 3.12. The workflows name the image and the Xcode
path explicitly rather than `-latest`.

**Scheduled workflows in a public repo** are disabled by GitHub after 60 days
with no repository activity. Publishing the pipeline counts as activity; if a
quiet stretch disables `poll.yml`, re-enable it with
`gh workflow enable poll.yml -R willoughby-apps/pipeline`.
