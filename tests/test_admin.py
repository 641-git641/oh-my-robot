from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from reviewbot.admin import _authorize
from reviewbot.config import Settings
from reviewbot.github_client import GitHubApiError
from reviewbot.main import create_app
from reviewbot.models import PullRequest, ReviewJob
from reviewbot.storage import QueueStore


class FakeAdminGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def get_pull_request(self, repository: str, number: int) -> PullRequest:
        self.calls.append((repository, number))
        return PullRequest(
            repository=repository,
            number=number,
            title="Manual review",
            state="open",
            head_sha="head-current",
            base_sha="base-current",
        )


class NotFoundAdminGitHub(FakeAdminGitHub):
    async def get_pull_request(self, repository: str, number: int) -> PullRequest:
        raise GitHubApiError("GET", f"/repos/{repository}/pulls/{number}", 404, "Not Found")


def _settings(tmp_path: Path, *, admin_token: str | None = "admin-secret") -> Settings:
    return Settings(
        github_token=SecretStr("github-token"),
        github_webhook_secret=SecretStr("webhook-secret"),
        github_repo_allowlist_raw="owner/repo",
        deepseek_api_key=SecretStr("deepseek-key"),
        admin_token=SecretStr(admin_token) if admin_token is not None else None,
        database_path=tmp_path / "robot.sqlite3",
        review_rule_file=tmp_path / "rules.md",
    )


def _job(delivery_id: str, *, number: int = 1) -> ReviewJob:
    return ReviewJob(
        delivery_id=delivery_id,
        event_type="pull_request",
        action="opened",
        repository="owner/repo",
        pull_request_number=number,
        webhook_head_sha=f"head-{delivery_id}",
    )


def _app(tmp_path: Path, *, admin_token: str | None = "admin-secret") -> tuple[object, QueueStore, FakeAdminGitHub]:
    settings = _settings(tmp_path, admin_token=admin_token)
    store = QueueStore(settings.database_path)
    store.initialize()
    github = FakeAdminGitHub()
    app = create_app(
        settings,
        github_client=github,  # type: ignore[arg-type]
        engine=object(),  # type: ignore[arg-type]
        store=store,
    )
    return app, store, github


@pytest.mark.asyncio
async def test_admin_requires_token_and_is_disabled_without_configuration(tmp_path: Path) -> None:
    app, _, _ = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://robot") as client:
        missing = await client.get("/admin/events")
        invalid = await client.get("/admin/events", headers={"Authorization": "Bearer wrong"})

    assert missing.status_code == 401
    assert invalid.status_code == 401

    disabled_app, _, _ = _app(tmp_path / "disabled", admin_token=None)
    disabled_transport = httpx.ASGITransport(app=disabled_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=disabled_transport, base_url="http://robot") as client:
        disabled = await client.get("/admin/events")
    assert disabled.status_code == 404


@pytest.mark.asyncio
async def test_admin_queries_filter_and_redact_nothing_sensitive(tmp_path: Path) -> None:
    app, store, _ = _app(tmp_path)
    assert store.enqueue(_job("failed-1"))
    assert store.claim_next() is not None
    store.mark_failed("failed-1", "safe failure", retry=False)
    store.record_review("owner/repo", 1, "head-reviewed", 99)
    store.record_review_metrics(
        "failed-1",
        diff_file_count=2,
        diff_bytes=100,
        omitted_file_count=1,
        model_input_tokens=10,
        model_output_tokens=5,
        finding_count=1,
        review_rank="P1",
        model_duration_ms=12.5,
        github_duration_ms=8.5,
    )

    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    headers = {"Authorization": "Bearer admin-secret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://robot") as client:
        events = await client.get("/admin/events?state=failed&limit=1", headers=headers)
        reviews = await client.get("/admin/reviews?repository=owner/repo", headers=headers)
        invalid_state = await client.get("/admin/events?state=unknown", headers=headers)
        invalid_cursor = await client.get("/admin/events?cursor=bad", headers=headers)
        huge_cursor = await client.get("/admin/events?cursor=9223372036854775808", headers=headers)
        valid_cursor = await client.get("/admin/events?cursor=0", headers=headers)
        metrics = await client.get("/admin/metrics", headers=headers)
        huge_pull_request_events = await client.get(
            "/admin/events?pull_request=9223372036854775808",
            headers=headers,
        )
        huge_pull_request_reviews = await client.get(
            "/admin/reviews?pull_request=9223372036854775808",
            headers=headers,
        )

    assert events.status_code == 200
    assert events.json()["items"][0]["delivery_id"] == "failed-1"
    assert "github-token" not in events.text
    assert "deepseek-key" not in events.text
    assert reviews.status_code == 200
    assert reviews.json()["items"][0]["comment_id"] == 99
    assert invalid_state.status_code == 400
    assert invalid_cursor.status_code == 400
    assert huge_cursor.status_code == 400
    assert valid_cursor.status_code == 200
    assert metrics.status_code == 200
    assert metrics.json()["reviewMetrics"]["modelInputTokens"] == 10
    assert huge_pull_request_events.status_code == 400
    assert huge_pull_request_reviews.status_code == 400


def test_admin_rejects_non_ascii_credentials_without_500() -> None:
    class RequestLike:
        headers = {"Authorization": "Bearer 密钥"}

    with pytest.raises(HTTPException) as raised:
        _authorize(RequestLike(), "admin-secret")  # type: ignore[arg-type]
    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_admin_replay_is_idempotent_and_preserves_source_event(tmp_path: Path) -> None:
    app, store, _ = _app(tmp_path)
    assert store.enqueue(_job("failed-1"))
    assert store.claim_next() is not None
    store.mark_failed("failed-1", "safe failure", retry=False)

    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    headers = {"Authorization": "Bearer admin-secret", "Idempotency-Key": "replay-once"}
    async with httpx.AsyncClient(transport=transport, base_url="http://robot") as client:
        first = await client.post("/admin/events/failed-1/replay", headers=headers)
        second = await client.post("/admin/events/failed-1/replay", headers=headers)
        conflict = await client.post("/admin/events/failed-1/replay", headers={"Authorization": "Bearer admin-secret"})

    assert first.status_code == 202
    assert first.json()["state"] == "queued"
    assert first.json()["sourceDeliveryId"] == "failed-1"
    assert second.status_code == 202
    assert second.json()["state"] == "duplicate"
    assert conflict.status_code == 202
    assert store.get_event(first.json()["deliveryId"]).source == "replay"


@pytest.mark.asyncio
async def test_admin_manual_review_fetches_current_head_and_checks_allowlist(tmp_path: Path) -> None:
    app, store, github = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    headers = {"Authorization": "Bearer admin-secret", "Idempotency-Key": "manual-once"}
    async with httpx.AsyncClient(transport=transport, base_url="http://robot") as client:
        response = await client.post("/admin/repos/owner/repo/pulls/7/review", headers=headers)
        duplicate = await client.post("/admin/repos/owner/repo/pulls/7/review", headers=headers)
        manual_delivery_id = response.json()["deliveryId"]
        store.mark_failed(manual_delivery_id, "manual failure", retry=False)
        replay = await client.post(
            f"/admin/events/{manual_delivery_id}/replay",
            headers={"Authorization": "Bearer admin-secret", "Idempotency-Key": "manual-replay"},
        )
        detail = await client.get(
            f"/admin/events/{manual_delivery_id}",
            headers={"Authorization": "Bearer admin-secret"},
        )
        forbidden = await client.post(
            "/admin/repos/other/repo/pulls/7/review",
            headers={"Authorization": "Bearer admin-secret"},
        )

    assert response.status_code == 202
    assert response.json()["headSha"] == "head-current"
    assert duplicate.status_code == 202
    assert duplicate.json()["state"] == "duplicate"
    assert replay.status_code == 202
    assert detail.status_code == 200
    assert forbidden.status_code == 403
    assert github.calls == [("owner/repo", 7), ("owner/repo", 7)]
    manual_event = store.get_event(response.json()["deliveryId"])
    assert manual_event is not None
    assert manual_event.source == "manual"
    assert manual_event.webhook_head_sha == "head-current"


@pytest.mark.asyncio
async def test_admin_manual_review_preserves_missing_pr_404(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = QueueStore(settings.database_path)
    store.initialize()
    app = create_app(
        settings,
        github_client=NotFoundAdminGitHub(),  # type: ignore[arg-type]
        engine=object(),  # type: ignore[arg-type]
        store=store,
    )
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://robot") as client:
        response = await client.post(
            "/admin/repos/owner/repo/pulls/404/review",
            headers={"Authorization": "Bearer admin-secret"},
        )

    assert response.status_code == 404
