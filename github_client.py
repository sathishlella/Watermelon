"""The single hand-written primitive: a raw GitHub REST client.

Everything the agent can *do* on GitHub ultimately flows through `request()`.
We deliberately do NOT hand-write one method per GitHub operation — that is the
"cheap version" the brief warns against. Instead the agent synthesises higher
level capabilities (add label, comment, assign, create file, ...) at runtime on
top of this primitive.

This module also classifies every error into a small set of *kinds*
(`rate_limit`, `validation`, `permission`, `not_found`, `http`). Those kinds are
what the agent turns into durable, structured constraints in capability memory.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from config import GITHUB_OWNER, GITHUB_REPO, GITHUB_TOKEN


@dataclass
class GitHubError(Exception):
    kind: str           # rate_limit | validation | permission | not_found | http
    message: str        # human-readable, safe to store as a constraint
    status: int
    body: dict | str | None = None

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"[{self.kind} {self.status}] {self.message}"


class GitHubClient:
    BASE = "https://api.github.com"

    def __init__(self) -> None:
        self.owner = GITHUB_OWNER
        self.repo = GITHUB_REPO
        self.api_call_count = 0  # cumulative; snapshot() per run
        self._http = httpx.Client(
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    def snapshot(self) -> int:
        return self.api_call_count

    def context(self) -> dict:
        """Values available for {owner}/{repo} substitution in path templates."""
        return {"owner": self.owner, "repo": self.repo}

    def request(self, method: str, path: str, params: dict | None = None,
                json: dict | None = None):
        """Make one real GitHub API call. Raises GitHubError on non-2xx."""
        url = self.BASE + path
        self.api_call_count += 1
        resp = self._http.request(method.upper(), url, params=params, json=json)

        if 200 <= resp.status_code < 300:
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

        raise self._classify(resp)

    def _classify(self, resp: httpx.Response) -> GitHubError:
        try:
            body = resp.json()
            msg = body.get("message", resp.text) if isinstance(body, dict) else resp.text
        except Exception:
            body, msg = resp.text, resp.text

        status = resp.status_code
        remaining = resp.headers.get("X-RateLimit-Remaining")

        if status in (403, 429) and remaining == "0":
            reset = resp.headers.get("X-RateLimit-Reset", "?")
            return GitHubError("rate_limit",
                               f"Rate limit exhausted; resets at epoch {reset}.",
                               status, body)
        if status == 422:
            # Surface field-level validation detail so it becomes a useful constraint.
            detail = ""
            if isinstance(body, dict) and body.get("errors"):
                detail = " " + "; ".join(
                    f"{e.get('field', '?')}: {e.get('code', e.get('message', '?'))}"
                    for e in body["errors"]
                )
            return GitHubError("validation", f"{msg}.{detail}".strip(), status, body)
        if status in (401, 403):
            return GitHubError("permission", msg, status, body)
        if status == 404:
            return GitHubError("not_found", msg, status, body)
        return GitHubError("http", msg, status, body)
