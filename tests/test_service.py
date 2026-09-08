from pathlib import Path

import pytest

from reviewbot.diff import DiffContext
from reviewbot.models import (
    ChangedFile,
    PullRequest,
    PullRequestComment,
    ReviewFinding,
    ReviewJob,
    ReviewResult,
)
from reviewbot.service import ReviewService
from reviewbot.storage import QueueStore


class FakeGitHub:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []
        self.inline_comments: list[dict[str, object]] = []
        self.check_runs: list[dict[str, object]] = []
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

    async def create_pull_request_review_comment(
        self,
        repository: str,
        number: int,
        **payload: object,
    ) -> int:
        self.inline_comments.append({"repository": repository, "number": number, **payload})
        return 100

    async def create_check_run(self, repository: str, **payload: object) -> int:
        self.check_runs.append({"repository": repository, **payload})
        return 200


class FakeEngine:
    async def review(self, pull_request: PullRequest, diff: DiffContext, rules: str) -> ReviewResult:
        assert pull_request.head_sha == "head-1"
        assert diff.has_changed_line("src/example.ts", 1)
        assert "rules" in rules
        return ReviewResult(summary="clean", verdict="clean", rank="P0")



class FindingEngine:
    async def review(self, pull_request: PullRequest, diff: DiffContext, rules: str) -> ReviewResult:
        del pull_request, rules
        assert diff.has_changed_line("src/example.ts", 1)
        return ReviewResult(
            summary="needs attention",
            verdict="needs_attention",
            rank="P1",
            findings=[
                ReviewFinding(
                    priority="P1",
                    path="src/example.ts",
                    line=1,
                    symbol="value",
                    title="Unsafe value",
                    problem="The changed value violates the contract.",
                    impact="The request can fail.",
                    suggestion="Validate the value before use.",
                    confidence=0.9,
                )
            ],
        )

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


@pytest.mark.asyncio
async def test_service_publishes_locatable_inline_finding_and_check_run(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "reviews.sqlite3")
    store.initialize()
    github = FakeGitHub()
    service = ReviewService(
        store=store,
        github=github,  # type: ignore[arg-type]
        engine=FindingEngine(),  # type: ignore[arg-type]
        rule_file=tmp_path / "rules.md",
        max_diff_bytes=10_000,
        max_review_bytes=10_000,
        allowlist=frozenset({"owner/repo"}),
    )
    (tmp_path / "rules.md").write_text("rules", encoding="utf-8")
    job = ReviewJob(
        delivery_id="finding-output",
        event_type="pull_request",
        action="opened",
        repository="owner/repo",
        pull_request_number=1,
    )
    assert store.enqueue(job)
    claimed = store.claim_next()
    assert claimed is not None

    await service.process(claimed[0])

    assert len(github.inline_comments) == 1
    assert "finding:" in str(github.inline_comments[0]["body"])
    assert github.inline_comments[0]["path"] == "src/example.ts"
    assert github.inline_comments[0]["line"] == 1
    assert github.check_runs == [
        {
            "repository": "owner/repo",
            "head_sha": "head-1",
            "name": "oh-my-robot review",
            "status": "completed",
            "conclusion": "failure",
            "summary": github.comments[0][2],
        }
    ]
