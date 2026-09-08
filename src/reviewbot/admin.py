from __future__ import annotations

import hashlib
import hmac
import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, status

from reviewbot.config import Settings
from reviewbot.github_client import GitHubApiError
from reviewbot.models import ReviewJob
from reviewbot.ports import GitHubPort
from reviewbot.storage import QueueStore

log = logging.getLogger(__name__)

_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_REPLAYABLE_STATES = frozenset({"failed", "skipped", "superseded"})
_EVENT_STATES = frozenset({"queued", "running", "succeeded", "failed", "skipped", "superseded"})


def register_admin_routes(
    app: FastAPI,
    *,
    settings: Settings,
    store: QueueStore,
    github: GitHubPort,
) -> None:
    if settings.admin_token is None:
        return
    token = settings.admin_token.get_secret_value().strip()
    if not token:
        return

    router = APIRouter(prefix="/admin")

    @router.get("/events")
    async def list_events(
        request: Request,
        state: str | None = None,
        repository: str | None = None,
        pull_request: int | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        _authorize(request, token)
        parsed_state = _parse_state(state)
        parsed_pull_request = _parse_pull_request_filter(pull_request)
        offset = _parse_cursor(cursor)
        try:
            events = await _to_thread(
                store.list_events,
                state=parsed_state,
                repository=repository,
                pull_request_number=parsed_pull_request,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return _page_response(events, limit=limit, offset=offset)

    @router.get("/events/{delivery_id:path}")
    async def get_event(request: Request, delivery_id: str) -> dict[str, Any]:
        _authorize(request, token)
        event = await _to_thread(store.get_event, delivery_id)
        if event is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")
        return _model_payload(event)

    @router.get("/reviews")
    async def list_reviews(
        request: Request,
        repository: str | None = None,
        pull_request: int | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        _authorize(request, token)
        parsed_pull_request = _parse_pull_request_filter(pull_request)
        offset = _parse_cursor(cursor)
        try:
            reviews = await _to_thread(
                store.list_reviews,
                repository=repository,
                pull_request_number=parsed_pull_request,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return _page_response(reviews, limit=limit, offset=offset)

    @router.get("/metrics")
    async def metrics(request: Request) -> dict[str, Any]:
        _authorize(request, token)
        return await _to_thread(store.metrics)

    @router.post("/events/{delivery_id:path}/replay", status_code=status.HTTP_202_ACCEPTED)
    async def replay_event(request: Request, delivery_id: str) -> dict[str, Any]:
        _authorize(request, token)
        event = await _to_thread(store.get_event, delivery_id)
        if event is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")
        if event.state not in _REPLAYABLE_STATES:
            raise HTTPException(status.HTTP_409_CONFLICT, f"event state {event.state!r} is not replayable")

        replay_id = _derived_delivery_id("replay", delivery_id, request.headers.get("Idempotency-Key"))
        replay_job = ReviewJob(
            delivery_id=replay_id,
            event_type=event.event_type,
            action=event.action,
            repository=event.repository,
            pull_request_number=event.pull_request_number,
            webhook_head_sha=event.webhook_head_sha,
        )
        inserted = await _to_thread(
            store.enqueue,
            replay_job,
            source="replay",
            source_delivery_id=delivery_id,
        )
        log.info(
            "admin_replay_enqueued",
            extra={
                "source_delivery_id": delivery_id,
                "delivery_id": replay_id,
                "state": "queued" if inserted else "duplicate",
            },
        )
        return {
            "state": "queued" if inserted else "duplicate",
            "deliveryId": replay_id,
            "sourceDeliveryId": delivery_id,
        }

    @router.post("/repos/{owner}/{repo}/pulls/{number}/review", status_code=status.HTTP_202_ACCEPTED)
    async def manual_review(request: Request, owner: str, repo: str, number: int) -> dict[str, Any]:
        _authorize(request, token)
        repository = _repository_name(owner, repo)
        if number <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pull Request number must be positive")
        if repository.lower() not in settings.repo_allowlist:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "repository not allowlisted")

        try:
            pull_request = await github.get_pull_request(repository, number)
        except GitHubApiError as exc:
            if exc.status_code == 404:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Pull Request not found") from exc
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitHub API unavailable") from exc
        if pull_request.state.lower() != "open":
            raise HTTPException(status.HTTP_409_CONFLICT, "Pull Request is not open")
        if pull_request.draft:
            raise HTTPException(status.HTTP_409_CONFLICT, "Pull Request is a draft")

        manual_id = _derived_delivery_id(
            "manual",
            f"{repository.lower()}:{number}",
            request.headers.get("Idempotency-Key"),
        )
        manual_job = ReviewJob(
            delivery_id=manual_id,
            event_type="pull_request",
            action="synchronize",
            repository=repository,
            pull_request_number=number,
            webhook_head_sha=pull_request.head_sha,
        )
        inserted = await _to_thread(store.enqueue, manual_job, source="manual")
        log.info(
            "manual_review_enqueued",
            extra={
                "delivery_id": manual_id,
                "repository": repository,
                "pull_request": number,
                "head_sha": pull_request.head_sha,
                "state": "queued" if inserted else "duplicate",
            },
        )
        return {
            "state": "queued" if inserted else "duplicate",
            "deliveryId": manual_id,
            "repository": repository,
            "pullRequest": number,
            "headSha": pull_request.head_sha,
        }

    app.include_router(router)


async def _to_thread(function, *args, **kwargs):
    import asyncio

    return await asyncio.to_thread(function, *args, **kwargs)


def _authorize(request: Request, expected_token: str) -> None:
    authorization = request.headers.get("Authorization", "")
    scheme, separator, provided_token = authorization.partition(" ")
    valid = (
        separator
        and scheme.lower() == "bearer"
        and hmac.compare_digest(provided_token.encode("utf-8"), expected_token.encode("utf-8"))
    )
    if not valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin credentials")


def _parse_state(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in _EVENT_STATES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid event state: {value!r}")
    return value


def _parse_pull_request_filter(value: int | None) -> int | None:
    if value is None:
        return None
    if value <= 0 or value > 9_223_372_036_854_775_807:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "pull_request must be a positive integer")
    return value

def _parse_cursor(value: str | None) -> int:
    if value is None or value == "":
        return 0
    try:
        cursor = int(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cursor must be a non-negative integer") from exc
    if cursor < 0 or cursor > 9_223_372_036_854_775_807:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cursor must be a non-negative integer")
    return cursor


def _page_response(items: list[Any], *, limit: int, offset: int) -> dict[str, Any]:
    if limit <= 0 or limit > 1_000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "limit must be between 1 and 1000")
    return {
        "items": [_model_payload(item) for item in items],
        "nextCursor": str(offset + limit) if len(items) == limit else None,
    }


def _model_payload(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _repository_name(owner: str, repo: str) -> str:
    if not _REPOSITORY_PART.fullmatch(owner) or not _REPOSITORY_PART.fullmatch(repo):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid repository")
    return f"{owner}/{repo}"


def _derived_delivery_id(kind: str, subject: str, idempotency_key: str | None) -> str:
    if idempotency_key:
        suffix = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    else:
        suffix = uuid.uuid4().hex
    return f"{kind}:{subject}:{suffix}"
