import pytest

from reviewbot.diff import build_diff_batches
from reviewbot.models import ChangedFile, PullRequest
from reviewbot.reviewer import ReviewEngine


def _file(name: str) -> ChangedFile:
    return ChangedFile(
        filename=name,
        patch="@@ -1 +1,2 @@\n+const changed = true",
        additions=1,
    )


def test_diff_batches_split_files_and_report_coverage() -> None:
    plan = build_diff_batches([_file("a.ts"), _file("b.ts"), _file("c.ts")], max_bytes=100)

    assert plan.total_files == 3
    assert plan.batch_count == 3
    assert plan.reviewed_files == ("a.ts", "b.ts", "c.ts")
    assert plan.omitted_files == ()
    assert all(batch.files for batch in plan.batches)


def test_diff_batches_mark_patchless_files_unavailable() -> None:
    plan = build_diff_batches([_file("a.ts"), ChangedFile(filename="logo.png")], max_bytes=10_000)

    assert plan.reviewed_files == ("a.ts",)
    assert plan.omitted_files == ("logo.png",)
    assert plan.patchless_files == ("logo.png",)


class BatchProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, max_tokens: int = 4_096) -> str:
        del messages, max_tokens
        self.calls += 1
        path = "a.ts" if self.calls == 1 else "b.ts"
        return (
            '{"summary":"batch", "verdict":"needs_attention", "rank":"P1",'
            f'"findings":[{{"priority":"P1","path":"{path}","line":1,"symbol":"run",'
            '"title":"Issue","problem":"p","impact":"i","suggestion":"s","confidence":0.9}],'
            '"test_suggestions":[]}'
        )


@pytest.mark.asyncio
async def test_review_engine_merges_batch_results() -> None:
    provider = BatchProvider()
    engine = ReviewEngine(provider)
    plan = build_diff_batches([_file("a.ts"), _file("b.ts")], max_bytes=100)
    result = await engine.review_plan(
        PullRequest(repository="owner/repo", number=1, title="Batch", head_sha="head", base_sha="base"),
        plan,
        "rules",
    )

    assert provider.calls == 2
    assert {finding.path for finding in result.result.findings} == {"a.ts", "b.ts"}
    assert result.result.verdict == "needs_attention"
