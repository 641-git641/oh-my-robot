from __future__ import annotations

import asyncio
import inspect
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status

from reviewbot.admin import register_admin_routes
from reviewbot.config import Settings, get_settings
from reviewbot.deepseek_client import DeepSeekClient
from reviewbot.github_client import GitHubClient
from reviewbot.logging_config import configure_logging
from reviewbot.omp_worker import OmpReadOnlyReviewer
from reviewbot.reviewer import ReviewEngine
from reviewbot.service import ReviewService
from reviewbot.storage import QueueStore
from reviewbot.webhook import (
    WebhookError,
    is_interaction_event,
    is_pull_request_event,
    is_review_action,
    parse_review_job,
    verify_webhook,
)
from reviewbot.worker import WorkerPool

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    github_client: GitHubClient | None = None,
    engine: ReviewEngine | None = None,
    store: QueueStore | None = None,
    roboomp_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    runtime = settings or get_settings()
    runtime.validate_runtime()
    configure_logging()
    queue_store = store or QueueStore(runtime.database_path)
    github = github_client or GitHubClient(
        base_url=runtime.api_base_url,
        token=runtime.github_token.get_secret_value(),  # type: ignore[union-attr]
        timeout_seconds=runtime.request_timeout_seconds,
    )
    deepseek = None
    deep_reviewer = None
    review_engine = engine
    if runtime.review_mode == "deep":
        deep_reviewer = OmpReadOnlyReviewer(
            command=runtime.omp_command,
            model=runtime.omp_model,
            timeout_seconds=runtime.deep_review_timeout_seconds,
        )
    elif review_engine is None:
        deepseek = DeepSeekClient(
            base_url=runtime.deepseek_base_url,
            api_key=runtime.deepseek_api_key.get_secret_value(),  # type: ignore[union-attr]
            model=runtime.deepseek_model,
            thinking_enabled=runtime.deepseek_thinking_enabled,
            reasoning_effort=runtime.deepseek_reasoning_effort,
            timeout_seconds=runtime.request_timeout_seconds,
            max_retries=runtime.max_retries,
        )
        review_engine = ReviewEngine(deepseek)

    service = ReviewService(
        store=queue_store,
        github=github,
        engine=review_engine,
        deep_reviewer=deep_reviewer,
        review_mode=runtime.review_mode,
        rule_file=runtime.review_rule_file,
        path_rule_file=runtime.review_path_rule_file,
        max_diff_bytes=runtime.max_diff_bytes,
        max_review_bytes=runtime.max_review_bytes,
        allowlist=runtime.repo_allowlist,
        enabled=runtime.review_enabled,
    )
    forward_roboomp = roboomp_client
    owns_roboomp_client = False
    if forward_roboomp is None and runtime.roboomp_webhook_url is not None:
        forward_roboomp = httpx.AsyncClient(timeout=runtime.roboomp_timeout_seconds)
        owns_roboomp_client = True
    worker = WorkerPool(store=queue_store, service=service, settings=runtime)
    stop_event = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop_event.clear()
        await asyncio.to_thread(queue_store.initialize)
        task = asyncio.create_task(worker.run(stop_event), name="review-worker")
        app.state.ready = True
        try:
            yield
        finally:
            stop_event.set()
            await asyncio.gather(task, return_exceptions=True)
            close_github = getattr(github, "close", None)
            if close_github is not None:
                result = close_github()
                if inspect.isawaitable(result):
                    await result
            if deepseek is not None:
                await deepseek.close()
            if owns_roboomp_client and forward_roboomp is not None:
                await forward_roboomp.aclose()
            app.state.ready = False

    app = FastAPI(title="oh-my-robot", version="0.1.0", lifespan=lifespan)
    app.state.ready = False
    app.state.settings = runtime
    app.state.store = queue_store
    register_admin_routes(app, settings=runtime, store=queue_store, github=github)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "reviewEnabled": runtime.review_enabled}

    @app.get("/readyz")
    async def readyz() -> dict[str, Any]:
        if not app.state.ready:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "robot is starting")
        return {"status": "ready", "queue": await asyncio.to_thread(queue_store.counts)}

    @app.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(request: Request) -> dict[str, Any]:
        delivery_id = request.headers.get("X-GitHub-Delivery") or ""
        event_type = request.headers.get("X-GitHub-Event", "")
        body = await request.body()
        log.info(
            "webhook_received",
            extra={
                "delivery_id": delivery_id,
                "event_type": event_type,
                "body_bytes": len(body),
            },
        )
        if len(body) > runtime.max_diff_bytes * 2:
            log.warning(
                "webhook_rejected",
                extra={"delivery_id": delivery_id, "event_type": event_type, "reason": "payload_too_large"},
            )
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "webhook payload too large")

        signature_header = request.headers.get("X-Hub-Signature-256")
        secret = runtime.github_webhook_secret.get_secret_value()  # type: ignore[union-attr]
        if not verify_webhook(body, secret=secret, signature_header=signature_header):
            log.warning(
                "webhook_rejected",
                extra={"delivery_id": delivery_id, "event_type": event_type, "reason": "invalid_signature"},
            )
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook signature")

        payload = _json_object(body)
        if is_interaction_event(event_type):
            if runtime.roboomp_webhook_url is None or forward_roboomp is None:
                log.info(
                    "webhook_skipped",
                    extra={
                        "delivery_id": delivery_id,
                        "event_type": event_type,
                        "reason": "roboomp_forwarding_not_configured",
                    },
                )
                return {"state": "skipped", "reason": "roboomp forwarding not configured"}
            return await _forward_interaction_event(
                client=forward_roboomp,
                target=str(runtime.roboomp_webhook_url),
                body=body,
                delivery_id=delivery_id,
                event_type=event_type,
                signature_header=signature_header,
            )
        if not is_pull_request_event(event_type, payload):
            log.info(
                "webhook_skipped",
                extra={"delivery_id": delivery_id, "event_type": event_type, "reason": "not_pull_request"},
            )
            return {"state": "skipped", "reason": "not a Pull Request event"}
        try:
            job = parse_review_job(
                body=body,
                event_type=event_type,
                delivery_id=delivery_id,
            )
        except WebhookError as exc:
            log.warning(
                "webhook_rejected",
                extra={"delivery_id": delivery_id, "event_type": event_type, "reason": "invalid_payload"},
            )
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        if job.repository.lower() not in runtime.repo_allowlist:
            log.info(
                "webhook_skipped",
                extra={
                    "delivery_id": job.delivery_id,
                    "repository": job.repository,
                    "reason": "repository_not_allowlisted",
                },
            )
            return {"state": "skipped", "reason": "repository not allowlisted"}
        if not runtime.review_enabled:
            log.info(
                "webhook_skipped",
                extra={"delivery_id": job.delivery_id, "repository": job.repository, "reason": "review_disabled"},
            )
            return {"state": "skipped", "reason": "review disabled"}
        if not is_review_action(job.action):
            log.info(
                "webhook_skipped",
                extra={"delivery_id": job.delivery_id, "repository": job.repository, "reason": "action_ignored"},
            )
            return {"state": "skipped", "reason": f"action {job.action!r} ignored"}


        inserted = await asyncio.to_thread(queue_store.enqueue, job)
        log.info(
            "job_enqueued",
            extra={
                "delivery_id": job.delivery_id,
                "repository": job.repository,
                "pull_request": job.pull_request_number,
                "action": job.action,
                "webhook_head_sha": job.webhook_head_sha,
                "state": "queued" if inserted else "duplicate",
            },
        )
        return {"state": "queued" if inserted else "duplicate", "deliveryId": job.delivery_id}

    return app


async def _forward_interaction_event(
    *,
    client: httpx.AsyncClient,
    target: str,
    body: bytes,
    delivery_id: str,
    event_type: str,
    signature_header: str | None,
) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": event_type,
    }
    if signature_header:
        headers["X-Hub-Signature-256"] = signature_header
    try:
        response = await client.post(target, content=body, headers=headers)
    except httpx.HTTPError as exc:
        log.error(
            "roboomp_forward_failed",
            extra={"delivery_id": delivery_id, "event_type": event_type, "error_type": exc.__class__.__name__},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "roboomp forwarding failed") from exc
    if not 200 <= response.status_code < 300:
        log.error(
            "roboomp_forward_rejected",
            extra={
                "delivery_id": delivery_id,
                "event_type": event_type,
                "http_status": response.status_code,
            },
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "roboomp rejected the webhook")
    return {"state": "forwarded", "target": "roboomp", "deliveryId": delivery_id}


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.bind_port)


if __name__ == "__main__":
    main()
