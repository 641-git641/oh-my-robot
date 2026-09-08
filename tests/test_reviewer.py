from collections.abc import Mapping, Sequence

import pytest

from reviewbot.diff import DiffContext
from reviewbot.models import PullRequest
from reviewbot.renderer import render_review
from reviewbot.reviewer import ReviewEngine, ReviewFormatError


class FakeCompletionProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[Sequence[Mapping[str, str]]] = []

    async def complete(self, messages: Sequence[Mapping[str, str]], *, max_tokens: int = 4_096) -> str:
        self.calls.append(messages)
        return self.responses.pop(0)


def pull_request() -> PullRequest:
    return PullRequest(
        repository="liu-huangmin/bjbh",
        number=1,
        title="Fix auth handling",
        head_sha="head-1",
        base_sha="base-1",
    )


def diff_context() -> DiffContext:
    return DiffContext(
        text="diff -- src/auth.ts\n@@ -1 +1,2 @@\n+throw new Error()",
        changed_lines={"src/auth.ts": frozenset({1})},
        omitted_files=(),
    )


@pytest.mark.asyncio
async def test_engine_repairs_one_invalid_json_response() -> None:
    provider = FakeCompletionProvider(
        [
            "not json",
            '{"summary":"Looks good","verdict":"clean","rank":"P0","findings":[],"test_suggestions":[]}',
        ]
    )
    engine = ReviewEngine(provider)

    result = await engine.review(pull_request(), diff_context(), "- Keep findings concrete")

    assert result.verdict == "clean"
    assert len(provider.calls) == 2
    assert "重新输出合法 JSON" in provider.calls[1][1]["content"]


@pytest.mark.asyncio
async def test_engine_requires_simplified_chinese_natural_language_output() -> None:
    provider = FakeCompletionProvider(
        ['{"summary":"审查通过","verdict":"clean","rank":"P3","findings":[],"test_suggestions":[]}']
    )

    await ReviewEngine(provider).review(pull_request(), diff_context(), "rules")

    system_prompt = provider.calls[0][0]["content"]
    user_prompt = provider.calls[0][1]["content"]
    assert "所有自然语言内容必须使用简体中文" in system_prompt
    assert '"summary": "用简体中文写 2-5 句技术总结"' in user_prompt

@pytest.mark.asyncio
async def test_engine_drops_findings_that_do_not_anchor_to_diff() -> None:
    provider = FakeCompletionProvider(
        [
            (
                '{"summary":"Potential issue","verdict":"needs_attention","rank":"P1",'
                '"findings":[{"priority":"P1","path":"src/other.ts","line":42,'
                '"symbol":"run","title":"Wrong path","problem":"p","impact":"i",'
                '"suggestion":"s","confidence":0.9}],"test_suggestions":[]}'
            )
        ]
    )

    result = await ReviewEngine(provider).review(pull_request(), diff_context(), "rules")

    assert result.findings == []
    assert "无法定位" in result.summary


@pytest.mark.asyncio
async def test_engine_rejects_unrepairable_output() -> None:
    provider = FakeCompletionProvider(["not json", "still not json"])

    with pytest.raises(ReviewFormatError):
        await ReviewEngine(provider).review(pull_request(), diff_context(), "rules")


def test_renderer_has_stable_marker_and_bounded_output() -> None:
    provider = FakeCompletionProvider([])
    del provider
    result_json = {
        "summary": "A concrete finding",
        "verdict": "needs_attention",
        "rank": "P1",
        "findings": [
            {
                "priority": "P1",
                "path": "src/auth.ts",
                "line": 1,
                "symbol": "login",
                "title": "Missing guard",
                "problem": "The error branch is not handled.",
                "impact": "The request can fail silently.",
                "suggestion": "Return the typed error and add a boundary test.",
                "confidence": 0.91,
            }
        ],
        "test_suggestions": ["Cover the error branch"],
    }
    from reviewbot.models import ReviewResult

    body = render_review(pull_request(), ReviewResult.model_validate(result_json), max_bytes=2_000)

    assert "<!-- oh-my-robot-review:head-1 -->" in body
    assert "src/auth.ts:1" in body
    assert "DeepSeek" in body
    assert len(body.encode()) <= 2_000
