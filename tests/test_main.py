import hashlib
import hmac
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from reviewbot.config import Settings
from reviewbot.main import create_app
from reviewbot.storage import QueueStore


@pytest.mark.asyncio
async def test_webhook_authenticates_and_enqueues_pull_request(tmp_path: Path) -> None:
    settings = Settings(
        github_token=SecretStr("github-token"),
        github_webhook_secret=SecretStr("webhook-secret"),
        github_repo_allowlist_raw="owner/repo",
        deepseek_api_key=SecretStr("deepseek-key"),
        database_path=tmp_path / "robot.sqlite3",
        review_rule_file=tmp_path / "rules.md",
    )
    store = QueueStore(settings.database_path)
    store.initialize()
    app = create_app(settings, github_client=object(), engine=object())  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    body = (
        b'{"action":"opened","repository":{"full_name":"owner/repo"},'
        b'"pull_request":{"number":7,"head":{"sha":"head-7"}}}'
    )

    signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(transport=transport, base_url="http://robot") as client:
        response = await client.post(
            "/webhook/github",
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-7",
            },
        )

    assert response.status_code == 202
    assert response.json() == {"state": "queued", "deliveryId": "delivery-7"}
    assert store.event_state("delivery-7") == "queued"


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_secret(tmp_path: Path) -> None:
    settings = Settings(
        github_token=SecretStr("github-token"),
        github_webhook_secret=SecretStr("webhook-secret"),
        github_repo_allowlist_raw="owner/repo",
        deepseek_api_key=SecretStr("deepseek-key"),
        database_path=tmp_path / "robot.sqlite3",
    )
    app = create_app(settings, github_client=object(), engine=object())  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://robot") as client:
        response = await client.post(
            "/webhook/github",
            content=b"{}",
            headers={"X-Hub-Signature-256": "sha256=wrong"},
        )

    assert response.status_code == 401
