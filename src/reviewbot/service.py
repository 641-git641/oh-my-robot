from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from reviewbot.deepseek_client import DeepSeekApiError
from reviewbot.diff import build_diff_context
from reviewbot.github_client import GitHubApiError
from reviewbot.models import ReviewJob
from reviewbot.ports import GitHubPort, ReviewPort
from reviewbot.renderer import render_review, review_marker
from reviewbot.reviewer import ReviewExecution, ReviewFormatError
from reviewbot.storage import QueueStore

log = logging.getLogger(__name__)

_DEFAULT_RULES = """# Review rules

- Review only the current Pull Request diff and the minimum surrounding code needed to prove a finding.
- Require a concrete failure mode, affected path, line, impact, and actionable modification suggestion.
- Flag security issues such as injection, credential leakage, unsafe dynamic evaluation, and missing authorization.
- Flag missing error handling, regressions, data loss, concurrency hazards, and observable contract breaks.
- Do not report formatting preferences or unrelated refactors as blocking findings.
"""


class ReviewService:
    """Application service with no dependency on FastAPI or process globals."""

    def __init__(
        self,
        *,
        store: QueueStore,
        github: GitHubPort,
        engine: ReviewPort,
        rule_file: Path,
        max_diff_bytes: int,
        max_review_bytes: int,
        allowlist: frozenset[str],
        enabled: bool = True,
    ) -> None:
        self._store = store
        self._github = github
        self._engine = engine
        self._rule_file = rule_file
        self._max_diff_bytes = max_diff_bytes
        self._max_review_bytes = max_review_bytes
        self._allowlist = allowlist
        self._enabled = enabled

    async def process(self, job: ReviewJob) -> None:
        if not self._enabled:
            await asyncio.to_thread(self._store.mark_skipped, job.delivery_id, "review disabled")
            return
        if job.repository.lower() not in self._allowlist:
            await asyncio.to_thread(self._store.mark_skipped, job.delivery_id, "repository not allowlisted")
            return
        if not self._is_review_action(job.action):
            await asyncio.to_thread(self._store.mark_skipped, job.delivery_id, "event action ignored")
            return

        github_duration_ms = 0.0
        pull_request, elapsed = await _timed_github(
            job.delivery_id,
            "get_pull_request",
            self._github.get_pull_request(job.repository, job.pull_request_number),
        )
        github_duration_ms += elapsed
        log.info(
            "review_started",
            extra={
                "delivery_id": job.delivery_id,
                "repository": pull_request.repository,
                "pull_request": pull_request.number,
                "action": job.action,
                "head_sha": pull_request.head_sha,
                "webhook_head_sha": job.webhook_head_sha,
            },
        )
        if pull_request.state.lower() != "open":
            await asyncio.to_thread(self._store.mark_skipped, job.delivery_id, "Pull Request is not open")
            return
        if pull_request.draft:
            await asyncio.to_thread(self._store.mark_skipped, job.delivery_id, "draft Pull Request")
            return
        if await asyncio.to_thread(
            self._store.has_review,
            pull_request.repository,
            pull_request.number,
            pull_request.head_sha,
        ):
            await asyncio.to_thread(self._store.mark_skipped, job.delivery_id, "head SHA already reviewed")
            return
        marker = review_marker(pull_request.head_sha)
        existing_comments, elapsed = await _timed_github(
            job.delivery_id,
            "list_pull_request_comments",
            self._github.list_pull_request_comments(
                pull_request.repository,
                pull_request.number,
            ),
        )
        github_duration_ms += elapsed
        existing = next((comment for comment in existing_comments if marker in comment.body), None)
        if existing is not None:
            await asyncio.to_thread(
                self._store.complete_review,
                job.delivery_id,
                pull_request.repository,
                pull_request.number,
                pull_request.head_sha,
                existing.id,
                diff_file_count=0,
                diff_bytes=0,
                omitted_file_count=0,
                model_input_tokens=None,
                model_output_tokens=None,
                finding_count=0,
                review_rank=None,
                model_duration_ms=0.0,
                github_duration_ms=github_duration_ms,
            )
            return

        changed_files, elapsed = await _timed_github(
            job.delivery_id,
            "list_pull_request_files",
            self._github.list_pull_request_files(pull_request.repository, pull_request.number),
        )
        github_duration_ms += elapsed
        diff = build_diff_context(changed_files, self._max_diff_bytes)
        rules = load_rules(self._rule_file)
        model_started = time.perf_counter()
        review_with_metadata = getattr(self._engine, "review_with_metadata", None)
        try:
            if callable(review_with_metadata):
                execution = await review_with_metadata(pull_request, diff, rules)
            else:
                execution = ReviewExecution(await self._engine.review(pull_request, diff, rules))
        except ReviewFormatError as exc:
            model_duration_ms = (time.perf_counter() - model_started) * 1000
            await asyncio.to_thread(
                self._store.record_review_attempt_metrics,
                job.delivery_id,
                diff_file_count=len(changed_files),
                diff_bytes=len(diff.text.encode("utf-8")),
                omitted_file_count=len(diff.omitted_files),
                model_input_tokens=exc.input_tokens,
                model_output_tokens=exc.output_tokens,
                model_duration_ms=model_duration_ms,
                github_duration_ms=github_duration_ms,
            )
            log.info(
                "model_request_completed",
                extra={
                    "delivery_id": job.delivery_id,
                    "success": False,
                    "model_input_tokens": exc.input_tokens,
                    "model_output_tokens": exc.output_tokens,
                    "model_duration_ms": round(model_duration_ms, 2),
                },
            )
            raise
        except Exception as exc:
            model_duration_ms = (time.perf_counter() - model_started) * 1000
            await asyncio.to_thread(
                self._store.record_review_attempt_metrics,
                job.delivery_id,
                diff_file_count=len(changed_files),
                diff_bytes=len(diff.text.encode("utf-8")),
                omitted_file_count=len(diff.omitted_files),
                model_input_tokens=getattr(exc, "input_tokens", None),
                model_output_tokens=getattr(exc, "output_tokens", None),
                model_duration_ms=model_duration_ms,
                github_duration_ms=github_duration_ms,
            )
            log.info(
                "model_request_completed",
                extra={
                    "delivery_id": job.delivery_id,
                    "success": False,
                    "model_input_tokens": getattr(exc, "input_tokens", None),
                    "model_output_tokens": getattr(exc, "output_tokens", None),
                    "model_duration_ms": round(model_duration_ms, 2),
                },
            )
            raise
        model_duration_ms = (time.perf_counter() - model_started) * 1000
        await asyncio.to_thread(
            self._store.record_review_attempt_metrics,
            job.delivery_id,
            diff_file_count=len(changed_files),
            diff_bytes=len(diff.text.encode("utf-8")),
            omitted_file_count=len(diff.omitted_files),
            model_input_tokens=execution.input_tokens,
            model_output_tokens=execution.output_tokens,
            model_duration_ms=model_duration_ms,
            github_duration_ms=github_duration_ms,
        )
        log.info(
            "model_request_completed",
            extra={
                "delivery_id": job.delivery_id,
                "success": True,
                "model_input_tokens": execution.input_tokens,
                "model_output_tokens": execution.output_tokens,
                "model_duration_ms": round(model_duration_ms, 2),
            },
        )
        result = execution.result

        latest_pull_request, elapsed = await _timed_github(
            job.delivery_id,
            "get_pull_request_latest",
            self._github.get_pull_request(job.repository, job.pull_request_number),
        )
        github_duration_ms += elapsed
        if latest_pull_request.state.lower() != "open":
            await asyncio.to_thread(self._store.mark_skipped, job.delivery_id, "Pull Request is no longer open")
            return
        if latest_pull_request.draft:
            await asyncio.to_thread(self._store.mark_skipped, job.delivery_id, "Pull Request became a draft")
            return
        if latest_pull_request.head_sha != pull_request.head_sha:
            refresh_job = ReviewJob(
                delivery_id=_refresh_delivery_id(
                    latest_pull_request.repository,
                    latest_pull_request.number,
                    latest_pull_request.head_sha,
                ),
                event_type="pull_request",
                action="synchronize",
                repository=latest_pull_request.repository,
                pull_request_number=latest_pull_request.number,
                webhook_head_sha=latest_pull_request.head_sha,
            )
            await asyncio.to_thread(
                self._store.supersede_and_enqueue,
                job.delivery_id,
                f"head SHA changed from {pull_request.head_sha} to {latest_pull_request.head_sha}",
                refresh_job,
            )
            log.info(
                "review_superseded",
                extra={
                    "delivery_id": job.delivery_id,
                    "repository": pull_request.repository,
                    "pull_request": pull_request.number,
                    "head_sha": pull_request.head_sha,
                    "latest_head_sha": latest_pull_request.head_sha,
                },
            )
            return

        comment = render_review(pull_request, result, max_bytes=self._max_review_bytes)
        comment_id, elapsed = await _timed_github(
            job.delivery_id,
            "create_pull_request_comment",
            self._github.create_pull_request_comment(
                pull_request.repository,
                pull_request.number,
                comment,
            ),
        )
        github_duration_ms += elapsed
        await asyncio.to_thread(
            self._store.complete_review,
            job.delivery_id,
            pull_request.repository,
            pull_request.number,
            pull_request.head_sha,
            comment_id,
            diff_file_count=len(changed_files),
            diff_bytes=len(diff.text.encode("utf-8")),
            omitted_file_count=len(diff.omitted_files),
            model_input_tokens=execution.input_tokens,
            model_output_tokens=execution.output_tokens,
            finding_count=len(result.findings),
            review_rank=result.rank,
            model_duration_ms=model_duration_ms,
            github_duration_ms=github_duration_ms,
        )
        log.info(
            "comment_created",
            extra={
                "delivery_id": job.delivery_id,
                "repository": pull_request.repository,
                "pull_request": pull_request.number,
                "comment_id": comment_id,
            },
        )
        log.info(
            "review_succeeded",
            extra={
                "delivery_id": job.delivery_id,
                "repository": pull_request.repository,
                "pull_request": pull_request.number,
                "head_sha": pull_request.head_sha,
                "finding_count": len(result.findings),
                "review_rank": result.rank,
                "diff_file_count": len(changed_files),
                "diff_bytes": len(diff.text.encode("utf-8")),
                "omitted_file_count": len(diff.omitted_files),
                "model_duration_ms": round(model_duration_ms, 2),
                "github_duration_ms": round(github_duration_ms, 2),
            },
        )

    @staticmethod
    def is_retryable(error: Exception) -> bool:
        if isinstance(error, ReviewFormatError):
            return False
        if isinstance(error, GitHubApiError):
            return (
                error.status_code == 0
                or error.status_code == 429
                or error.status_code >= 500
                or (error.status_code == 403 and error.rate_limited)
            )
        if isinstance(error, DeepSeekApiError):
            return error.status_code == 0 or error.status_code == 429 or error.status_code >= 500
        return False

    @staticmethod
    def _is_review_action(action: str) -> bool:
        return action in {"opened", "reopened", "ready_for_review", "synchronize"}


async def _timed_github(
    delivery_id: str,
    operation: str,
    awaitable: Awaitable[Any],
) -> tuple[Any, float]:
    started = time.perf_counter()
    try:
        result = await awaitable
    except Exception as exc:
        duration_ms = (time.perf_counter() - started) * 1000
        log.info(
            "github_request_completed",
            extra={
                "delivery_id": delivery_id,
                "operation": operation,
                "duration_ms": round(duration_ms, 2),
                "http_status": getattr(exc, "status_code", None),
                "retryable": ReviewService.is_retryable(exc),
                "error_type": exc.__class__.__name__,
            },
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    log.info(
        "github_request_completed",
        extra={
            "delivery_id": delivery_id,
            "operation": operation,
            "duration_ms": round(duration_ms, 2),
            "success": True,
        },
    )
    return result, duration_ms

def _refresh_delivery_id(repository: str, number: int, head_sha: str) -> str:
    return f"refresh:{repository.lower()}:{number}:{head_sha}"


def load_rules(path: Path) -> str:
    try:
        rules = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _DEFAULT_RULES
    except OSError:
        return _DEFAULT_RULES
    return rules.strip() or _DEFAULT_RULES
