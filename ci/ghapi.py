"""A tiny GitHub REST client for the pipeline's own scripts (stdlib only).

Every body is decoded by its Content-Encoding: urllib never decompresses, and
servers in front of APIs gzip regardless of what was asked (monorepo lesson,
2026-09-15). An error message names the method, path and status, never the
token.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import urllib.error
import urllib.request
import zlib

API = "https://api.github.com"
ORG = "willoughby-apps"
PIPELINE_REPO = f"{ORG}/pipeline"

# The pipeline's GitHub App, willoughby-apps-bot. Every job that writes to a
# guest repo mints its own installation token for that one repo
# (actions/create-github-app-token) and passes it as APP_TOKEN. Statuses,
# comments and issues it writes carry this login as `creator` / `user`: a
# guest can write statuses on their own repo, but never as this bot.
# Verified 2026-09-18: GET /users/willoughby-apps-bot[bot] -> id 331092058, type Bot.
APP_TOKEN_ENV = "APP_TOKEN"
BOT_LOGIN = "willoughby-apps-bot[bot]"

# A guest repo name as onboarding creates it: <guest>-<app>, lower case.
REPO_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
TAG_RE = re.compile(r"v[0-9]{1,4}(\.[0-9]{1,4}){0,2}")
KINDS = ("push", "tag")
# Org repos that are never a guest repo, and so never in a guest token's scope.
RESERVED_REPOS = ("pipeline", "app-template", "start")


class GitHubError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, message: str):
        super().__init__(f"{method} {path} -> HTTP {status}: {message[:300]}")
        self.status = status


def decode_body(raw: bytes, encoding: str | None) -> bytes:
    enc = (encoding or "").strip().lower()
    if enc in ("", "identity"):
        return raw
    if enc in ("gzip", "x-gzip"):
        return gzip.decompress(raw)
    if enc == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    raise ValueError(f"unsupported Content-Encoding {encoding!r}")


def next_link(link_header: str | None) -> str | None:
    for part in (link_header or "").split(","):
        m = re.search(r'<([^>]+)>\s*;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None


def validate_inputs(repo: str, sha: str, kind: str, tag: str) -> None:
    """The check workflow's inputs, checked before any of them is used."""
    if not REPO_NAME_RE.fullmatch(repo or "") or repo in RESERVED_REPOS:
        raise ValueError(f"not a guest repo name: {repo!r}")
    if not SHA_RE.fullmatch(sha or ""):
        raise ValueError("sha must be a full 40-character lower-case commit SHA")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if kind == "tag" and not TAG_RE.fullmatch(tag or ""):
        raise ValueError("a tag check needs a tag like v1.2")
    if kind == "push" and tag:
        raise ValueError("a push check takes no tag")


class Client:
    def __init__(self, token: str | None, api: str = API, opener=None):
        self.token = token
        self.api = api
        self._open = opener or urllib.request.urlopen

    @classmethod
    def from_env(cls, var: str) -> "Client":
        token = os.environ.get(var, "")
        if not token:
            if var == APP_TOKEN_ENV:
                raise SystemExit(f"{var} is not set: this job mints it with actions/create-github-app-token "
                                 f"(secret APP_PRIVATE_KEY, variable APP_CLIENT_ID on {PIPELINE_REPO})")
            raise SystemExit(f"{var} is not set: add it as a secret of {PIPELINE_REPO}")
        return cls(token)

    def _request(self, method: str, path: str, body=None, accept="application/vnd.github+json"):
        url = path if path.startswith("https://") else self.api + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", accept)
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "willoughby-apps-pipeline")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._open(req, timeout=60) as resp:
                raw = decode_body(resp.read(), resp.headers.get("Content-Encoding"))
                return resp.status, resp.headers, raw
        except urllib.error.HTTPError as e:
            raw = decode_body(e.read() or b"", e.headers.get("Content-Encoding") if e.headers else None)
            raise GitHubError(method, path.split("?")[0], e.code, raw.decode("utf-8", "replace")) from None

    def json(self, method: str, path: str, body=None):
        _, _, raw = self._request(method, path, body)
        return json.loads(raw) if raw.strip() else None

    def get(self, path: str):
        return self.json("GET", path)

    def post(self, path: str, body):
        return self.json("POST", path, body)

    def delete(self, path: str):
        return self.json("DELETE", path)

    def paginate(self, path: str, key: str | None = None, limit: int = 1000):
        """Every item across pages (`key` picks the list out of a wrapped response)."""
        out = []
        url = path + ("&" if "?" in path else "?") + "per_page=100"
        while url and len(out) < limit:
            _, headers, raw = self._request("GET", url)
            page = json.loads(raw)
            out.extend(page[key] if key else page)
            url = next_link(headers.get("Link"))
        return out[:limit]

    def download(self, path: str, dest, max_bytes: int, accept="application/vnd.github+json"):
        """Stream a body (following GitHub's redirect) to `dest`, refusing past `max_bytes`."""
        url = self.api + path
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", accept)
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "willoughby-apps-pipeline")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        total = 0
        try:
            with self._open(req, timeout=300) as resp, open(dest, "wb") as fh:
                if (resp.headers.get("Content-Encoding") or "identity").lower() != "identity":
                    raise ValueError("an archive download came back content-encoded")
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"download is over the {max_bytes} byte cap")
                    fh.write(chunk)
        except urllib.error.HTTPError as e:
            raise GitHubError("GET", path, e.code, (e.read() or b"").decode("utf-8", "replace")) from None
        return total
