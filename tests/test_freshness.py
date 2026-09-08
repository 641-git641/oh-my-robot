from pathlib import Path

import pytest

from reviewbot.diff import DiffContext
from reviewbot.github_client import GitHubApiError
from reviewbot.models import ChangedFile, PullRequest, PullRequestComment, ReviewJob, ReviewResult
from reviewbot.service import ReviewService
from reviewbot.storage import QueueStore


class SequencedGitHub:
    def __init__(self, pull_requests: list[PullRequest]) -> None:
        self._pull_requests = pull_requests
        self._get_index = 0
        self.comments: list[tuple[str, int, str]] = []

    async def get_pull_request(self, repository: str, number: int) -> PullRequest:
        del repository, number
        index = min(self._get_index, len(self._pull_requests) - 1)
        self._get_index += 1
        return self._pull_requests[index]

    async def list_pull_request_files(self, repository: str, number: int) -> list[ChangedFile]:
        del repository, number
        return [ChangedFile(filename="src/example.py", patch="@@ -1 +1,2 @@\n+value = 1", additions=1)]

    async def list_pull_request_comments(self, repository: str, number: int) -> list[PullRequestComment]:
        del repository, number
        return []

    async def create_pull_request_comment(self, repository: str, number: int, body: str) -> int:
        self.comments.append((repository, number, body))
        return 123


class RecordingEngine:
    def __init__(self) -> None:
        self.heads: list[str] = []

    async def review(self, pull_request: PullRequest, diff: DiffContext, rules: str) -> ReviewResult:
        del diff, rules
        self.heads.append(pull_request.head_sha)
        return ReviewResult(summary="clean", verdict="clean", rank="P0")


def _pull_request(head_sha: str, *, state: str = "open", draft: bool = False) -> PullRequest:
    return PullRequest(
        repository="owner/repo",
        number=1,
        title="Example",
        state=state,
        draft=draft,
        head_sha=head_sha,
        base_sha="base-1",
    )


def _service(store: QueueStore, github: SequencedGitHub, engine: RecordingEngine, rules: Path) -> ReviewService:
    return ReviewService(
        store=store,
        github=github,  # type: ignore[arg-type]
        engine=engine,  # type: ignore[arg-type]
        rule_file=rules,
        max_diff_bytes=10_000,
        max_review_bytes=10_000,
        allowlist=frozenset({"owner/repo"}),
    )


def _job(delivery_id: str) -> ReviewJob:
    return ReviewJob(
        delivery_id=delivery_id,
        event_type="pull_request",
        action="synchronize",
        repository="owner/repo",
        pull_request_number=1,
        webhook_head_sha="head-a",
    )


@pytest.mark.asyncio
async def test_stale_head_is_superseded_and_refresh_is_reviewed(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "reviews.sqlite3")
    store.initialize()
    rules = tmp_path / "rules.md"
    rules.write_text("rules", encoding="utf-8")
    github = SequencedGitHub([_pull_request("head-a"), _pull_request("head-b"), _pull_request("head-b")])
    engine = RecordingEngine()
    service = _service(store, github, engine, rules)
    assert store.enqueue(_job("delivery-a"))

    first_claim = store.claim_next()
    assert first_claim is not None
    await service.process(first_claim[0])

    assert github.comments == []
    assert store.event_state("delivery-a") == "superseded"
    refresh_events = [event for event in store.list_events() if event.source == "refresh"]
    assert len(refresh_events) == 1
    assert refresh_events[0].delivery_id == "refresh:owner/repo:1:head-b"
    assert refresh_events[0].source_delivery_id == "delivery-a"

    refresh_claim = store.claim_next()
    assert refresh_claim is not None
    await service.process(refresh_claim[0])

    assert len(github.comments) == 1
    assert store.has_review("owner/repo", 1, "head-b")
    assert store.event_state(refresh_events[0].delivery_id) == "succeeded"
    assert engine.heads == ["head-a", "head-b"]


@pytest.mark.asyncio
async def test_duplicate_stale_tasks_share_one_refresh_job(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "reviews.sqlite3")
    store.initialize()
    rules = tmp_path / "rules.md"
    rules.write_text("rules", encoding="utf-8")
    github = SequencedGitHub(
        [
            _pull_request("head-a"),
            _pull_request("head-b"),
            _pull_request("head-a"),
            _pull_request("head-b"),
        ]
    )
    service = _service(store, github, RecordingEngine(), rules)
    assert store.enqueue(_job("delivery-a"))
    assert store.enqueue(_job("delivery-a-2"))

    first_claim = store.claim_next()
    assert first_claim is not None
    await service.process(first_claim[0])
    second_claim = store.claim_next()
    assert second_claim is not None
    await service.process(second_claim[0])

    refresh_events = [event for event in store.list_events() if event.source == "refresh"]
    assert len(refresh_events) == 1
    assert store.event_state("delivery-a") == "superseded"
    assert store.event_state("delivery-a-2") == "superseded"


@pytest.mark.asyncio
async def test_closed_pr_after_review_is_skipped_without_refresh(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "reviews.sqlite3")
    store.initialize()
    rules = tmp_path / "rules.md"
    rules.write_text("rules", encoding="utf-8")
    github = SequencedGitHub([_pull_request("head-a"), _pull_request("head-a", state="closed")])
    service = _service(store, github, RecordingEngine(), rules)
    assert store.enqueue(_job("delivery-closed"))

    claim = store.claim_next()
    assert claim is not None
    await service.process(claim[0])

    assert store.event_state("delivery-closed") == "skipped"
    assert not [event for event in store.list_events() if event.source == "refresh"]
    assert github.comments == []


def test_only_rate_limited_github_403_is_retryable() -> None:
    limited = GitHubApiError(
        "GET",
        "/repos/owner/repo/pulls/1",
        403,
        "API rate limit exceeded",
        rate_limited=True,
    )
    forbidden = GitHubApiError(
        "GET",
        "/repos/owner/repo/pulls/1",
        403,
        "Resource not accessible",
        rate_limited=False,
    )

    assert ReviewService.is_retryable(limited)
    assert not ReviewService.is_retryable(forbidden)


@pytest.mark.asyncio
async def test_draft_pr_after_review_is_skipped_without_refresh(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "reviews.sqlite3")
    store.initialize()
    rules = tmp_path / "rules.md"
    rules.write_text("rules", encoding="utf-8")
    github = SequencedGitHub([_pull_request("head-a"), _pull_request("head-a", draft=True)])
    service = _service(store, github, RecordingEngine(), rules)
    assert store.enqueue(_job("delivery-draft"))

    claim = store.claim_next()
    assert claim is not None
    await service.process(claim[0])

    assert store.event_state("delivery-draft") == "skipped"
    assert not [event for event in store.list_events() if event.source == "refresh"]
    assert github.comments == []


class FailingLatestGitHub(SequencedGitHub):
    async def get_pull_request(self, repository: str, number: int) -> PullRequest:
        if self._get_index == 1:
            raise GitHubApiError("GET", "/repos/owner/repo/pulls/1", 503, "upstream unavailable")
        return await super().get_pull_request(repository, number)


@pytest.mark.asyncio
async def test_latest_head_fetch_failure_propagates_without_publishing(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "reviews.sqlite3")
    store.initialize()
    rules = tmp_path / "rules.md"
    rules.write_text("rules", encoding="utf-8")
    github = FailingLatestGitHub([_pull_request("head-a")])
    service = _service(store, github, RecordingEngine(), rules)
    assert store.enqueue(_job("delivery-error"))

    claim = store.claim_next()
    assert claim is not None
    with pytest.raises(GitHubApiError):
        await service.process(claim[0])

    assert store.event_state("delivery-error") == "running"
    assert github.comments == []


@pytest.mark.asyncio
async def test_superseded_refresh_head_can_be_requeued_after_force_push_bounce(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "reviews.sqlite3")
    store.initialize()
    rules = tmp_path / "rules.md"
    rules.write_text("rules", encoding="utf-8")
    github = SequencedGitHub(
        [
            _pull_request("head-a"),
            _pull_request("head-b"),
            _pull_request("head-b"),
            _pull_request("head-a"),
            _pull_request("head-a"),
            _pull_request("head-b"),
        ]
    )
    service = _service(store, github, RecordingEngine(), rules)
    assert store.enqueue(_job("delivery-a"))

    for expected_delivery in (
        "delivery-a",
        "refresh:owner/repo:1:head-b",
        "refresh:owner/repo:1:head-a",
    ):
        claim = store.claim_next()
        assert claim is not None
        assert claim[0].delivery_id == expected_delivery
        await service.process(claim[0])

    events = {event.delivery_id: event for event in store.list_events(limit=20)}
    assert events["refresh:owner/repo:1:head-b"].state == "queued"
    assert events["refresh:owner/repo:1:head-a"].state == "superseded"


def test_refresh_does_not_requeue_a_head_with_completed_review(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "reviews.sqlite3")
    store.initialize()
    assert store.enqueue(_job("delivery-old"))
    assert store.claim_next() is not None
    store.record_review("owner/repo", 1, "head-b", 99)

    refresh_job = ReviewJob(
        delivery_id="refresh:owner/repo:1:head-b",
        event_type="pull_request",
        action="synchronize",
        repository="owner/repo",
        pull_request_number=1,
        webhook_head_sha="head-b",
    )
    assert not store.supersede_and_enqueue("delivery-old", "head changed", refresh_job)
    assert store.event_state("delivery-old") == "superseded"
    assert not [event for event in store.list_events() if event.delivery_id == refresh_job.delivery_id]
