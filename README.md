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

1. **fetch** (Ubuntu). The only job before `report` that holds a token for
   the guest repo (the app's, contents read, that one repo), and it installs nothing but the SHA-256-pinned `age` and parses nothing a
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
| `APP_PRIVATE_KEY` (+ variable `APP_CLIENT_ID`) | `poll`, `fetch`, `report`, `request` | the private key of the GitHub App **willoughby-apps-bot** (client ID `Iv23liiTpvQBZ5cbNj7a`, installed on the guest repos only, never this one). Never used directly: each of those jobs mints its own installation token with `actions/create-github-app-token`, **limited to the one guest repo** it works on and to the permissions that job needs (table below), and the token is revoked when the job ends. |
| `MONOREPO_DISPATCH` | `request` | fine-grained PAT on `ajcohen9/willoughby`: Actions write. |
| `PIPELINE_PRIVATE_KEY` | `report` | the age identity for `keys/pipeline.age.pub` (`guest-apps/scripts/pipeline_keypair.sh`). The Mini holds the same key to decrypt the unsigned build. |

The tokens each job mints (`tests/test_pipeline_workflows.py` pins this table):

| Job | Repositories | Permissions |
|---|---|---|
| `poll` (1st token) | none named (all, read-only) | organization custom properties read, metadata read: lists the guest repos (without metadata the token is scoped to no repo and sees only this public one: measured 2026-09-18) |
| `poll` (2nd token) | exactly the guest repos the 1st listed | contents read, commit statuses write, organization custom properties read |
| `fetch` | the one guest repo (`inputs.repo`, validated first) | contents read, metadata read |
| `report` | the one guest repo | contents write (the previews ref, the commit comment), commit statuses write, metadata read |
| `request` | the one guest repo | issues write, metadata read |

The app itself has Administration write (the future enroll job adds
collaborators with it), but no token minted here asks for it, and no token
here names this repo, `app-template` or `start`, where the app is not
installed at all. The pipeline's statuses,
comments and issues are therefore written by **`willoughby-apps-bot[bot]`**
(`ci/ghapi.py BOT_LOGIN`, checked against `GET /users/willoughby-apps-bot[bot]`
on 2026-09-18: a `Bot`, id 331092058), and that login is what every consumer
matches `creator` / `user` against. An installation token cannot call
`GET /user`, so the scripts never ask who they are.

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
guest code. The fetch job (which holds the app token) encrypts the
tarball and the tree listing to a key pair it makes for this run only, uploads
the ciphertext with 1-day retention, and passes the one-run private key to
`gate` and `build` as a job output; `report` deletes the artifact at the end of
the run. Alternatives
rejected: a token in the build job (even a one-repo app token is a credential
that guest code could use or keep); `actions/checkout` with a token (a secret in a guest-code
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
decrypted `unsigned.ipa` against a status created by the pipeline's bot
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
Andrew's own user. That ruleset is repository-level (organization rulesets
need GitHub Team; this org is on Free), and whoever holds Administration write
on this repo can delete it, change the default branch or add a collaborator
(GitHub's "Permissions required for GitHub Apps" lists the ruleset, repository
and collaborator endpoints under Administration write). The app has
Administration write, so **the app is not installed on this repo**, nor on
`app-template` or `start`: its installation is "Only select repositories",
the guest repos alone, each added when it is onboarded. A stolen
`APP_PRIVATE_KEY` therefore reaches the guest repos and nothing here; it can
neither touch this repo's ruleset nor push to its main. That is enforced, not
assumed: the Mini's signing job reads the installation as the app before
anything else and signs nothing while it is on all repositories or on a
reserved one, `publish_pipeline.sh` refuses to push in that state, onboarding
refuses to start in it, and no token the Mini mints may name a reserved repo
(`guest-apps/common/github.py`). The poll job's listing token, which names no
repo, carries only organization custom properties read and metadata read, and
sees only what the installation reaches.

**Actions.** GitHub-owned only (org policy), each pinned by full commit SHA.

**Runner images** (from `actions/runner-images`, 2026-09-18): `macos-26` is
macOS 26 arm64 with Xcode 26.6 as the default and the iOS 26.5 simulators;
`ubuntu-24.04` has Python 3.12. The workflows name the image and the Xcode
path explicitly rather than `-latest`.

**Scheduled workflows in a public repo** are disabled by GitHub after 60 days
with no repository activity. Publishing the pipeline counts as activity; if a
quiet stretch disables `poll.yml`, re-enable it with
`gh workflow enable poll.yml -R willoughby-apps/pipeline`.
