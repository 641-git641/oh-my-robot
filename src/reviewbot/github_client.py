from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from reviewbot.models import ChangedFile, PullRequest, PullRequestComment

_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_GITHUB_ACCEPT = "application/vnd.github+json"
_GITHUB_API_VERSION = "2022-11-28"
_PAGE_SIZE = 100

class GitHubApiError(RuntimeError):
    def __init__(
        self,
        method: str,
        path: str,
        status_code: int,
        message: str = "GitHub API request failed",
        *,
        retry_after: float | None = None,
        rate_limited: bool = False,
        rate_limit_remaining: int | None = None,
        rate_limit_reset: float | None = None,
    ) -> None:
        super().__init__(f"{method} {path} returned HTTP {status_code}: {message}")
        self.method = method
        self.path = path
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after
        self.rate_limited = rate_limited
        self.rate_limit_remaining = rate_limit_remaining
        self.rate_limit_reset = rate_limit_reset


class GitHubClient:
    """Minimal GitHub REST adapter used by the review application."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 90.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Accept": _GITHUB_ACCEPT,
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "oh-my-robot/0.1",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def download_pull_request_archive(
        self,
        repository: str,
        head_sha: str,
        *,
        max_bytes: int = 50_000_000,
    ) -> bytes:
        owner, name = self._split_repository(repository)
        path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/zipball/{quote(head_sha, safe='')}"
        try:
            async with self._client.stream("GET", path.lstrip("/")) as response:
                if response.is_error:
                    await response.aread()
                    message = _error_message(response)
                    retry_after, rate_limit_remaining, rate_limit_reset = _rate_limit_metadata(response)
                    raise GitHubApiError(
                        "GET",
                        path,
                        response.status_code,
                        message,
                        retry_after=retry_after,
                        rate_limited=_is_rate_limited(response, message),
                        rate_limit_remaining=rate_limit_remaining,
                        rate_limit_reset=rate_limit_reset,
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise GitHubApiError("GET", path, 413, "archive exceeds size limit")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:
            raise GitHubApiError("GET", path, 0, "network error") from exc

    async def get_pull_request(self, repository: str, number: int) -> PullRequest:
        path = self._pull_path(repository, number)
        payload = await self._request("GET", path)
        return self._parse_pull_request(repository, number, payload)

    async def list_pull_request_files(self, repository: str, number: int) -> list[ChangedFile]:
        path = f"{self._pull_path(repository, number)}/files"
        files: list[ChangedFile] = []
        page = 1
        while True:
            payload = await self._request(
                "GET",
                path,
                params={"per_page": _PAGE_SIZE, "page": page},
            )
            if not isinstance(payload, list):
                raise GitHubApiError("GET", path, 200, "unexpected files response")
            batch = [self._parse_changed_file(item) for item in payload if isinstance(item, Mapping)]
            files.extend(batch)
            if len(payload) < _PAGE_SIZE:
                return files
            page += 1

    async def list_pull_request_comments(self, repository: str, number: int) -> list[PullRequestComment]:
        path = self._comments_path(repository, number)
        comments: list[PullRequestComment] = []
        page = 1
        while True:
            payload = await self._request(
                "GET",
                path,
                params={"per_page": _PAGE_SIZE, "page": page},
            )
            if not isinstance(payload, list):
                raise GitHubApiError("GET", path, 200, "unexpected comments response")
            batch = [self._parse_comment(item) for item in payload if isinstance(item, Mapping)]
            comments.extend(batch)
            if len(payload) < _PAGE_SIZE:
                return comments
            page += 1

    async def create_pull_request_comment(self, repository: str, number: int, body: str) -> int | None:
        path = self._comments_path(repository, number)
        payload = await self._request("POST", path, json_body={"body": body})
        if isinstance(payload, Mapping) and isinstance(payload.get("id"), int):
            return payload["id"]
        return None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                path.lstrip("/"),
                params=params,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise GitHubApiError(method, path, 0, "network error") from exc

        if response.is_error:
            message = _error_message(response)
            retry_after, rate_limit_remaining, rate_limit_reset = _rate_limit_metadata(response)
            raise GitHubApiError(
                method,
                path,
                response.status_code,
                message,
                retry_after=retry_after,
                rate_limited=_is_rate_limited(response, message),
                rate_limit_remaining=rate_limit_remaining,
                rate_limit_reset=rate_limit_reset,
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubApiError(method, path, response.status_code, "invalid JSON response") from exc

    @staticmethod
    def _pull_path(repository: str, number: int) -> str:
        owner, name = GitHubClient._split_repository(repository)
        if number <= 0:
            raise ValueError("Pull Request number must be positive")
        return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/pulls/{number}"

    @staticmethod
    def _comments_path(repository: str, number: int) -> str:
        owner, name = GitHubClient._split_repository(repository)
        if number <= 0:
            raise ValueError("Pull Request number must be positive")
        return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/issues/{number}/comments"

    @staticmethod
    def _split_repository(repository: str) -> tuple[str, str]:
        parts = repository.strip().split("/", 1)
        if len(parts) != 2 or not all(_REPOSITORY_PART.fullmatch(part) for part in parts):
            raise ValueError(f"invalid GitHub repository: {repository!r}")
        return parts[0], parts[1]

    @staticmethod
    def _parse_pull_request(repository: str, number: int, payload: Any) -> PullRequest:
        if not isinstance(payload, Mapping):
            raise GitHubApiError("GET", "pull request", 200, "unexpected pull request response")
        head = payload.get("head") if isinstance(payload.get("head"), Mapping) else {}
        base = payload.get("base") if isinstance(payload.get("base"), Mapping) else {}
        user = payload.get("user") if isinstance(payload.get("user"), Mapping) else {}
        return PullRequest(
            repository=repository,
            number=number,
            title=str(payload.get("title") or "Untitled Pull Request"),
            body=str(payload.get("body") or ""),
            state=str(payload.get("state") or "open"),
            draft=bool(payload.get("draft")),
            author=str(user.get("login") or "unknown"),
            head_sha=GitHubClient._first_text(head.get("sha")),
            head_ref=GitHubClient._first_text(head.get("ref")),
            base_sha=GitHubClient._first_text(base.get("sha")),
            base_ref=GitHubClient._first_text(base.get("ref"), "main"),
            html_url=GitHubClient._first_text(payload.get("html_url")),
        )

    @staticmethod
    def _parse_changed_file(payload: Mapping[str, Any]) -> ChangedFile:
        filename = GitHubClient._first_text(payload.get("filename"))
        if not filename:
            raise GitHubApiError("GET", "pull request files", 200, "file entry has no path")
        return ChangedFile(
            filename=filename,
            status=str(payload.get("status") or "modified"),
            patch=GitHubClient._first_text(payload.get("patch")),
            additions=GitHubClient._integer(payload.get("additions")),
            deletions=GitHubClient._integer(payload.get("deletions")),
        )

    @staticmethod
    def _parse_comment(payload: Mapping[str, Any]) -> PullRequestComment:
        comment_id = payload.get("id")
        if not isinstance(comment_id, int):
            raise GitHubApiError("GET", "pull request comments", 200, "comment entry has no id")
        return PullRequestComment(id=comment_id, body=GitHubClient._first_text(payload.get("body")))

    @staticmethod
    def _first_text(*values: Any) -> str:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _integer(value: Any) -> int:
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str):
            try:
                parsed = int(value.strip())
            except ValueError:
                return 0
            return parsed if parsed >= 0 else 0
        return 0


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "request rejected"
    if isinstance(payload, Mapping):
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return "request rejected"


def _rate_limit_metadata(response: httpx.Response) -> tuple[float | None, int | None, float | None]:
    retry_after: float | None = None
    retry_after_header = response.headers.get("retry-after")
    if retry_after_header:
        try:
            retry_after = max(0.0, float(retry_after_header))
        except ValueError:
            retry_after = None

    rate_limit_reset: float | None = None
    reset_header = response.headers.get("x-ratelimit-reset")
    if reset_header:
        try:
            rate_limit_reset = float(reset_header)
        except ValueError:
            rate_limit_reset = None

    remaining_header = response.headers.get("x-ratelimit-remaining")
    try:
        rate_limit_remaining = int(remaining_header) if remaining_header is not None else None
    except ValueError:
        rate_limit_remaining = None
    if retry_after is None and rate_limit_remaining == 0 and rate_limit_reset is not None:
        retry_after = max(0.0, rate_limit_reset - time.time())
    return retry_after, rate_limit_remaining, rate_limit_reset


def _is_rate_limited(response: httpx.Response, message: str) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if response.headers.get("retry-after") is not None:
        return True
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    lowered = message.lower()
    return "rate limit" in lowered or "abuse detection" in lowered
