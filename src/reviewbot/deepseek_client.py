from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class CompletionResult:
    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class DeepSeekApiError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"DeepSeek API failed with HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None


class DeepSeekClient:
    """Small, stateless DeepSeek Chat Completions adapter."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        thinking_enabled: bool = False,
        reasoning_effort: str = "high",
        timeout_seconds: float = 90.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._thinking_enabled = thinking_enabled
        self._reasoning_effort = reasoning_effort
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "oh-my-robot/0.1",
            },
            timeout=timeout_seconds,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def complete(self, messages: Sequence[Mapping[str, str]], *, max_tokens: int = 4_096) -> str:
        result = await self.complete_with_usage(messages, max_tokens=max_tokens)
        return result.content

    async def complete_with_usage(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        max_tokens: int = 4_096,
    ) -> CompletionResult:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),
            "stream": False,
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        payload["thinking"] = (
            {
                "type": "enabled",
                "reasoning_effort": self._reasoning_effort,
            }
            if self._thinking_enabled
            else {"type": "disabled"}
        )

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post("/chat/completions", json=payload)
            except httpx.HTTPError as exc:
                if attempt >= self._max_retries:
                    raise DeepSeekApiError(0, "network error") from exc
                await asyncio.sleep(_retry_delay(attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self._max_retries:
                    await asyncio.sleep(_retry_delay(attempt))
                    continue
            if response.is_error:
                raise DeepSeekApiError(response.status_code, "request rejected")
            try:
                response_payload = response.json()
            except ValueError as exc:
                raise DeepSeekApiError(response.status_code, "invalid JSON response") from exc
            content = _message_content(response_payload)
            if not content:
                raise DeepSeekApiError(response.status_code, "response has no message content")
            input_tokens, output_tokens = _usage(response_payload)
            return CompletionResult(content, input_tokens, output_tokens)

        raise DeepSeekApiError(500, "request exhausted retries")


def _message_content(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, Mapping):
        return ""
    message = first.get("message")
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""


def _usage(payload: Any) -> tuple[int | None, int | None]:
    usage = payload.get("usage") if isinstance(payload, Mapping) else None
    if not isinstance(usage, Mapping):
        return None, None
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    return _token_count(input_tokens), _token_count(output_tokens)


def _token_count(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and value >= 0 else None


def _retry_delay(attempt: int) -> float:
    return min(2.0**attempt, 8.0)
