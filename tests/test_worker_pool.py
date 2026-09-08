import asyncio
from pathlib import Path

import pytest

from reviewbot.config import Settings
from reviewbot.github_client import GitHubApiError
from reviewbot.models import ReviewJob
from reviewbot.storage import QueueStore
from reviewbot.worker import WorkerPool


class ControlledService:
    def __init__(self, store: QueueStore, *, fail_delivery: str | None = None) -> None:
        self._store = store
        self._fail_delivery = fail_delivery
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.completed = asyncio.Event()
        self.active = 0
        self.max_active = 0
        self.completed_count = 0

    def start_event(self, delivery_id: str) -> asyncio.Event:
        return self.started.setdefault(delivery_id, asyncio.Event())

    async def process(self, job: ReviewJob) -> None:
        self.start_event(job.delivery_id).set()
        release = self.release.setdefault(job.delivery_id, asyncio.Event())
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if job.delivery_id == self._fail_delivery:
                raise RuntimeError("expected failure")
            await release.wait()
            self._store.mark_succeeded(job.delivery_id)
            self.completed_count += 1
            self.completed.set()
        finally:
            self.active -= 1

    @staticmethod
    def is_retryable(error: Exception) -> bool:
        del error
        return False


class RetryOnceService:
    def __init__(self, store: QueueStore) -> None:
        self._store = store
        self.started: dict[str, asyncio.Event] = {}
        self.calls: dict[str, int] = {}

    def start_event(self, delivery_id: str) -> asyncio.Event:
        return self.started.setdefault(delivery_id, asyncio.Event())

    async def process(self, job: ReviewJob) -> None:
        self.start_event(job.delivery_id).set()
        self.calls[job.delivery_id] = self.calls.get(job.delivery_id, 0) + 1
        if job.delivery_id == "a-retry" and self.calls[job.delivery_id] == 1:
            raise GitHubApiError("GET", "/repos/owner/repo/pulls/1", 503, "temporary")
        self._store.mark_succeeded(job.delivery_id)

    @staticmethod
    def is_retryable(error: Exception) -> bool:
        return isinstance(error, GitHubApiError)


def _settings(*, max_concurrency: int = 2, drain: float = 1.0, max_retries: int = 0) -> Settings:
    return Settings(
        max_concurrency=max_concurrency,
        shutdown_drain_seconds=drain,
        max_retries=max_retries,
    )


def _job(delivery_id: str, number: int) -> ReviewJob:
    return ReviewJob(
        delivery_id=delivery_id,
        event_type="pull_request",
        action="opened",
        repository="owner/repo",
        pull_request_number=number,
        webhook_head_sha=f"head-{delivery_id}",
    )


async def _start_pool(
    store: QueueStore,
    service: ControlledService,
    settings: Settings,
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    stop_event = asyncio.Event()
    pool_task = asyncio.create_task(WorkerPool(store=store, service=service, settings=settings).run(stop_event))
    return stop_event, pool_task


async def _stop_pool(stop_event: asyncio.Event, pool_task: asyncio.Task[None]) -> None:
    stop_event.set()
    await asyncio.wait_for(pool_task, timeout=2)


@pytest.mark.asyncio
async def test_different_pull_requests_run_concurrently(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "robot.sqlite3")
    store.initialize()
    assert store.enqueue(_job("delivery-1", 1))
    assert store.enqueue(_job("delivery-2", 2))
    service = ControlledService(store)
    stop_event, pool_task = await _start_pool(store, service, _settings())

    await asyncio.wait_for(service.start_event("delivery-1").wait(), timeout=1)
    await asyncio.wait_for(service.start_event("delivery-2").wait(), timeout=1)
    assert service.max_active == 2

    service.release["delivery-1"].set()
    service.release["delivery-2"].set()
    await asyncio.wait_for(service.completed.wait(), timeout=1)
    await _stop_pool(stop_event, pool_task)
    assert store.event_state("delivery-1") == "succeeded"
    assert store.event_state("delivery-2") == "succeeded"


@pytest.mark.asyncio
async def test_same_pull_request_is_serialized_by_database_claim(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "robot.sqlite3")
    store.initialize()
    first = _job("delivery-1", 1)
    second = _job("delivery-2", 1)
    assert store.enqueue(first)
    assert store.enqueue(second)
    service = ControlledService(store)
    stop_event, pool_task = await _start_pool(store, service, _settings())

    await asyncio.wait_for(service.start_event("delivery-1").wait(), timeout=1)
    await asyncio.sleep(0)
    assert "delivery-2" not in service.started
    assert service.max_active == 1

    service.release["delivery-1"].set()
    await asyncio.wait_for(service.start_event("delivery-2").wait(), timeout=1)
    service.release["delivery-2"].set()
    await asyncio.wait_for(service.completed.wait(), timeout=1)
    await _stop_pool(stop_event, pool_task)
    assert store.event_state("delivery-1") == "succeeded"
    assert store.event_state("delivery-2") == "succeeded"


@pytest.mark.asyncio
async def test_one_failed_task_does_not_stop_other_tasks(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "robot.sqlite3")
    store.initialize()
    assert store.enqueue(_job("delivery-fail", 1))
    assert store.enqueue(_job("delivery-ok", 2))
    service = ControlledService(store, fail_delivery="delivery-fail")
    service.release["delivery-ok"] = asyncio.Event()
    service.release["delivery-ok"].set()
    stop_event, pool_task = await _start_pool(store, service, _settings(max_concurrency=1))

    await asyncio.wait_for(service.start_event("delivery-ok").wait(), timeout=1)
    await asyncio.wait_for(service.completed.wait(), timeout=1)
    await _stop_pool(stop_event, pool_task)
    assert store.event_state("delivery-fail") == "failed"
    assert store.event_state("delivery-ok") == "succeeded"


@pytest.mark.asyncio
async def test_shutdown_drains_active_tasks(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "robot.sqlite3")
    store.initialize()
    assert store.enqueue(_job("delivery-1", 1))
    service = ControlledService(store)
    stop_event, pool_task = await _start_pool(store, service, _settings(drain=1))

    await asyncio.wait_for(service.start_event("delivery-1").wait(), timeout=1)
    stop_event.set()
    service.release["delivery-1"].set()
    await asyncio.wait_for(pool_task, timeout=2)
    assert store.event_state("delivery-1") == "succeeded"


@pytest.mark.asyncio
async def test_shutdown_timeout_cancels_active_and_requeues_task(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "robot.sqlite3")
    store.initialize()
    assert store.enqueue(_job("delivery-stuck", 1))
    service = ControlledService(store)
    stop_event, pool_task = await _start_pool(store, service, _settings(drain=0.01))

    await asyncio.wait_for(service.start_event("delivery-stuck").wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(pool_task, timeout=1)
    assert store.event_state("delivery-stuck") == "queued"


@pytest.mark.asyncio
async def test_retry_backoff_releases_slot_before_reclaim(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "robot.sqlite3")
    store.initialize()
    assert store.enqueue(_job("a-retry", 1))
    assert store.enqueue(_job("z-ok", 2))
    service = RetryOnceService(store)
    stop_event, pool_task = await _start_pool(store, service, _settings(max_concurrency=1, max_retries=1))

    try:
        await asyncio.wait_for(service.start_event("z-ok").wait(), timeout=1)
        retry_event = store.get_event("a-retry")
        assert retry_event is not None
        assert service.calls["a-retry"] == 1
        assert retry_event.state == "queued"
        assert retry_event.available_at > retry_event.updated_at
        assert store.event_state("z-ok") == "succeeded"
    finally:
        await _stop_pool(stop_event, pool_task)
