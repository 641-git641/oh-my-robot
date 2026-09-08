from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from reviewbot.models import ReviewJob

REVIEW_ACTIONS = frozenset(
    {
        "opened",
        "reopened",
        "ready_for_review",
        "synchronize",
    }
)
INTERACTION_EVENTS = frozenset(
    {
        "issue_comment",
        "issues",
        "pull_request_review",
        "pull_request_review_comment",
    }
)


class WebhookError(ValueError):
    pass


def verify_webhook(body: bytes, *, secret: str, signature_header: str | None) -> bool:
    """Verify GitHub's HMAC-SHA256 webhook signature without timing leaks."""
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def parse_review_job(
    *,
    body: bytes,
    event_type: str,
    delivery_id: str | None,
) -> ReviewJob:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise WebhookError("invalid webhook JSON") from exc
    if not isinstance(payload, Mapping):
        raise WebhookError("webhook payload must be an object")
    if not is_pull_request_event(event_type, payload):
        raise WebhookError("unsupported GitHub event")

    repository = _repository_name(payload)
    pr = _pull_request_object(payload)
    number = _first_int(payload.get("number"), pr.get("number"))
    if not repository or number is None or number <= 0:
        raise WebhookError("webhook payload has no repository and Pull Request number")

    return ReviewJob(
        delivery_id=delivery_id or hashlib.sha256(body).hexdigest(),
        event_type=event_type.strip().lower() or "pull_request",
        action=_normalize_action(payload),
        repository=repository,
        pull_request_number=number,
        webhook_head_sha=_head_sha(pr),
    )


def is_pull_request_event(event_type: str, payload: Mapping[str, Any]) -> bool:
    del payload
    normalized = event_type.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized == "pull_request"

def is_interaction_event(event_type: str) -> bool:
    normalized = event_type.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized in INTERACTION_EVENTS


def is_review_action(action: str) -> bool:
    return action in REVIEW_ACTIONS


def _pull_request_object(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    candidate = payload.get("pull_request")
    return candidate if isinstance(candidate, Mapping) else {}


def _repository_name(payload: Mapping[str, Any]) -> str:
    repository = payload.get("repository")
    if not isinstance(repository, Mapping):
        return ""
    full_name = repository.get("full_name")
    if isinstance(full_name, str) and full_name.strip():
        return full_name.strip()
    owner = repository.get("owner")
    owner_login = owner.get("login") if isinstance(owner, Mapping) else None
    name = repository.get("name")
    if isinstance(owner_login, str) and owner_login.strip() and isinstance(name, str) and name.strip():
        return f"{owner_login.strip()}/{name.strip()}"
    return ""


def _normalize_action(payload: Mapping[str, Any]) -> str:
    raw = payload.get("action")
    if not isinstance(raw, str) or not raw.strip():
        return "opened"
    return raw.strip().lower().replace(" ", "_").replace("-", "_")


def _head_sha(pr: Mapping[str, Any]) -> str:
    head = pr.get("head")
    if not isinstance(head, Mapping):
        return ""
    sha = head.get("sha")
    return sha.strip() if isinstance(sha, str) else ""


def _first_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None
