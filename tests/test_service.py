from pathlib import Path

import pytest

from reviewbot.diff import DiffContext
from reviewbot.models import ChangedFile, PullRequest, PullRequestComment, ReviewJob, ReviewResult
from reviewbot.service import ReviewService
from reviewbot.storage import QueueStore


class FakeGitHub:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []

    async def get_pull_request(self, repository: str, number: int) -> PullRequest:
        return PullRequest(
            repository=repository,
            number=number,
            title="Test PR",
            head_sha="head-1",
            base_sha="base-1",
        )

    async def list_pull_request_files(self, repository: str, number: int) -> list[ChangedFile]:
        return [
            ChangedFile(
                filename="src/example.ts",
                patch="@@ -1 +1,2 @@\n+const value = 1",
                additions=1,
            )
        ]

    async def list_pull_request_comments(self, repository: str, number: int) -> list[PullRequestComment]:
        return []

    async def create_pull_request_comment(self, repository: str, number: int, body: str) -> int:
        self.comments.append((repository, number, body))
        return 99


class FakeEngine:
    async def review(self, pull_request: PullRequest, diff: DiffContext, rules: str) -> ReviewResult:
        assert pull_request.head_sha == "head-1"
        assert diff.has_changed_line("src/example.ts", 1)
        assert "rules" in rules
        return ReviewResult(summary="clean", verdict="clean", rank="P0")


@pytest.mark.asyncio
async def test_service_is_independent_from_fastapi_and_is_idempotent(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "reviews.sqlite3")
    store.initialize()
    github = FakeGitHub()
    service = ReviewService(
        store=store,
        github=github,  # type: ignore[arg-type]
        engine=FakeEngine(),  # type: ignore[arg-type]
        rule_file=tmp_path / "rules.md",
        max_diff_bytes=10_000,
        max_review_bytes=10_000,
        allowlist=frozenset({"owner/repo"}),
    )
    (tmp_path / "rules.md").write_text("rules", encoding="utf-8")
    job = ReviewJob(
        delivery_id="delivery-1",
        event_type="pull_request",
        action="opened",
        repository="owner/repo",
        pull_request_number=1,
    )
    assert store.enqueue(job)

    claimed = store.claim_next()
    assert claimed is not None
    await service.process(claimed[0])

    assert len(github.comments) == 1
    assert store.event_state("delivery-1") == "succeeded"
    assert store.has_review("owner/repo", 1, "head-1")
    metrics = store.metrics()
    assert metrics["reviewMetrics"]["reviewCount"] == 1
    assert metrics["reviewMetrics"]["diffFileCount"] == 1

    duplicate = ReviewJob(
        delivery_id="delivery-2",
        event_type="pull_request",
        action="synchronize",
        repository="owner/repo",
        pull_request_number=1,
    )
    assert store.enqueue(duplicate)
    claimed_duplicate = store.claim_next()
    assert claimed_duplicate is not None
    await service.process(claimed_duplicate[0])
    assert len(github.comments) == 1
    assert store.event_state("delivery-2") == "skipped"
