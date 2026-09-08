from __future__ import annotations

from typing import Protocol

from reviewbot.diff import DiffContext
from reviewbot.models import ChangedFile, PullRequest, PullRequestComment, ReviewResult


class GitHubPort(Protocol):
    async def get_pull_request(self, repository: str, number: int) -> PullRequest: ...

    async def list_pull_request_files(self, repository: str, number: int) -> list[ChangedFile]: ...

    async def create_pull_request_comment(self, repository: str, number: int, body: str) -> int | None: ...

    async def list_pull_request_comments(self, repository: str, number: int) -> list[PullRequestComment]: ...


class ReviewPort(Protocol):
    async def review(self, pull_request: PullRequest, diff: DiffContext, rules: str) -> ReviewResult: ...
