import json

import httpx
import pytest

from reviewbot.deepseek_client import DeepSeekClient


@pytest.mark.asyncio
async def test_deepseek_client_sends_json_mode_without_exposing_key_in_request_path() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"summary":"clean"}'}},
                ]
            },
        )

    client = DeepSeekClient(
        base_url="https://api.deepseek.com",
        api_key="runtime-only-key",
        model="deepseek-chat",
        transport=httpx.MockTransport(handler),
    )
    try:
        content = await client.complete([{"role": "user", "content": "review"}])
    finally:
        await client.close()

    assert content == '{"summary":"clean"}'
    assert seen["path"] == "/chat/completions"
    assert seen["query"] == b""
    assert seen["authorization"] == "Bearer runtime-only-key"
    assert '"response_format":{"type":"json_object"}' in str(seen["body"])
    request_body = json.loads(str(seen["body"]))
    assert request_body["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_deepseek_client_composes_with_review_engine() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"summary":"Potential issue","verdict":"needs_attention","rank":"P1",'
                                '"findings":[{"priority":"P1","path":"src/example.ts","line":1,'
                                '"symbol":"run","title":"Missing guard","problem":"p","impact":"i",'
                                '"suggestion":"s","confidence":0.8}],"test_suggestions":[]}'
                            )
                        }
                    }
                ]
            },
        )

    from reviewbot.diff import DiffContext
    from reviewbot.models import PullRequest
    from reviewbot.reviewer import ReviewEngine

    client = DeepSeekClient(
        base_url="https://api.deepseek.com",
        api_key="runtime-only-key",
        model="deepseek-chat",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await ReviewEngine(client).review(
            PullRequest(
                repository="owner/repo",
                number=1,
                title="Example",
                head_sha="head-1",
                base_sha="base-1",
            ),
            DiffContext(
                text="@@ -1 +1,2 @@\n+const value = 1",
                changed_lines={"src/example.ts": frozenset({1})},
                omitted_files=(),
            ),
            "rules",
        )
    finally:
        await client.close()

    assert result.rank == "P1"
    assert result.findings[0].path == "src/example.ts"


@pytest.mark.asyncio
async def test_deepseek_client_exposes_token_usage_without_shared_state() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"summary":"clean"}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    client = DeepSeekClient(
        base_url="https://api.deepseek.com",
        api_key="runtime-only-key",
        model="deepseek-chat",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.complete_with_usage([{"role": "user", "content": "review"}])
    finally:
        await client.close()

    assert result.content == '{"summary":"clean"}'
    assert result.input_tokens == 11
    assert result.output_tokens == 7
