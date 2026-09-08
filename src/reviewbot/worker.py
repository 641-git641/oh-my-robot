from __future__ import annotations

import asyncio
import logging

from reviewbot.config import Settings
from reviewbot.deepseek_client import DeepSeekApiError
from reviewbot.github_client import GitHubApiError
from reviewbot.reviewer import ReviewFormatError
from reviewbot.security import redact_sensitive_text
from reviewbot.service import ReviewService
from reviewbot.storage import QueueStore

log = logging.getLogger(__name__)


class WorkerPool:
    def __init__(self, *, store: QueueStore, service: ReviewService, settings: Settings) -> None:
        self._store = store
        self._service = service
        self._settings = settings

    async def run(self, stop_event: asyncio.Event) -> None:
        tasks: set[asyncio.Task[None]] = set()
        try:
            while not stop_event.is_set():
                _collect_finished(tasks)
                if len(tasks) >= self._settings.max_concurrency:
                    await _wait_for_task(tasks, stop_event=stop_event)
                    continue

                claimed = await asyncio.to_thread(self._store.claim_next)
                if claimed is None:
                    if tasks:
                        await _wait_for_task(tasks, stop_event=stop_event, timeout=1.0)
                    else:
                        await _wait_for_work(stop_event)
                    continue

                job, attempts = claimed
                tasks.add(
                    asyncio.create_task(
                        self._run_claimed(job, attempts),
                        name=f"review:{job.delivery_id}",
                    )
                )
        finally:
            await _drain(tasks, self._settings.shutdown_drain_seconds)

    async def _run_claimed(self, job, attempts: int) -> None:
        log.info(
            "job_claimed",
            extra={
                "delivery_id": job.delivery_id,
                "repository": job.repository,
                "pull_request": job.pull_request_number,
                "attempts": attempts,
            },
        )
        try:
            await self._service.process(job)
            event = await asyncio.to_thread(self._store.get_event, job.delivery_id)
            log.info(
                "job_succeeded",
                extra={
                    "delivery_id": job.delivery_id,
                    "repository": job.repository,
                    "pull_request": job.pull_request_number,
                    "attempts": attempts,
                    "state": event.state if event is not None else "unknown",
                    "source": event.source if event is not None else "unknown",
                    "head_sha": event.webhook_head_sha if event is not None else job.webhook_head_sha,
                    "error_type": event.error_type if event is not None else None,
                },
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self._store.requeue_if_running,
                job.delivery_id,
                "worker cancelled",
                error_type="worker",
            )
            raise
        except Exception as exc:
            retry = attempts <= self._settings.max_retries and self._service.is_retryable(exc)
            error = _safe_error(exc)
            error_type = _error_type(exc)
            server_retry_after = getattr(exc, "retry_after", None) if retry else None
            retry_after = (
                server_retry_after if server_retry_after is not None else min(2.0**attempts, 30.0)
            ) if retry else None
            await asyncio.to_thread(
                self._store.mark_failed,
                job.delivery_id,
                error,
                retry=retry,
                retry_after=retry_after if retry else None,
                error_type=error_type,
            )
            event = "job_retried" if retry else "job_failed"
            log.log(
                logging.WARNING if retry else logging.ERROR,
                event,
                extra={
                    "delivery_id": job.delivery_id,
                    "repository": job.repository,
                    "pull_request": job.pull_request_number,
                    "attempts": attempts,
                    "retry": retry,
                    "retry_after": retry_after if retry else None,
                    "error_type": error_type,
                    "error": error,
                },
            )


def _collect_finished(tasks: set[asyncio.Task[None]]) -> None:
    finished = {task for task in tasks if task.done()}
    for task in finished:
        tasks.remove(task)
        if task.cancelled():
            continue
        exception = task.exception()
        if exception is not None:
            _log_task_exception(exception)


async def _wait_for_task(
    tasks: set[asyncio.Task[None]],
    *,
    stop_event: asyncio.Event | None = None,
    timeout: float | None = None,
) -> None:
    if not tasks:
        return
    stop_task = asyncio.create_task(stop_event.wait()) if stop_event is not None else None
    wait_set = set(tasks)
    if stop_task is not None:
        wait_set.add(stop_task)  # type: ignore[arg-type]
    done, _ = await asyncio.wait(wait_set, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    if stop_task is not None and not stop_task.done():
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
    for task in done:
        if task is stop_task:
            continue
        tasks.discard(task)
        if task.cancelled():
            continue
        exception = task.exception()
        if exception is not None:
            _log_task_exception(exception)


async def _drain(tasks: set[asyncio.Task[None]], timeout: float) -> None:
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in done:
        tasks.discard(task)
        if not task.cancelled():
            exception = task.exception()
            if exception is not None:
                _log_task_exception(exception)
    if not pending:
        return
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    tasks.clear()


async def _wait_for_work(stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=1.0)
    except TimeoutError:
        return


def _log_task_exception(exception: BaseException) -> None:
    log.error(
        "review task exited unexpectedly",
        exc_info=(type(exception), exception, exception.__traceback__),
    )

def _error_type(error: Exception) -> str:
    if isinstance(error, GitHubApiError):
        return "github"
    if isinstance(error, (DeepSeekApiError, ReviewFormatError)):
        return "model"
    return error.__class__.__name__

def _safe_error(error: Exception) -> str:
    message = redact_sensitive_text(str(error).replace("\r", " ").replace("\n", " ").strip())
    return message[:500] or error.__class__.__name__
