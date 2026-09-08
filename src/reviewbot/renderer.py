from __future__ import annotations

import html

from reviewbot.diff import DiffReviewPlan
from reviewbot.models import PullRequest, ReviewFinding, ReviewResult
from reviewbot.security import redact_sensitive_text

_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def review_marker(head_sha: str) -> str:
    return f"<!-- oh-my-robot-review:{head_sha} -->"


def render_inline_finding(pull_request: PullRequest, finding: ReviewFinding, fingerprint: str) -> str:
    return "\n".join(
        [
            review_marker(pull_request.head_sha),
            f"<!-- finding:{html.escape(fingerprint, quote=True)} -->",
            *_render_finding(finding),
        ]
    ).strip()

def render_review(
    pull_request: PullRequest,
    result: ReviewResult,
    *,
    max_bytes: int,
    coverage: DiffReviewPlan | None = None,
) -> str:
    marker = review_marker(pull_request.head_sha)
    verdict = "通过初步审查" if result.verdict == "clean" else "需要关注"
    lines = [
        marker,
        "## AI Review",
        "",
        f"**结论：** {verdict}；**建议级别：** {result.rank}",
        "",
        _safe_text(result.summary),
    ]

    if coverage is not None:
        lines.extend(
            [
                "",
                "### Review 覆盖范围",
                f"- changed files：{coverage.total_files}",
                f"- reviewed files：{len(coverage.reviewed_files)}",
                f"- omitted files：{len(coverage.omitted_files)}",
                f"- review batches：{coverage.batch_count}",
            ]
        )

    findings = sorted(
        result.findings, key=lambda finding: (_PRIORITY_ORDER[finding.priority], finding.path, finding.line)
    )
    if findings:
        lines.extend(["", "### 发现的问题"])
        for finding in findings:
            lines.extend(_render_finding(finding))

    if result.test_suggestions:
        lines.extend(["", "### 测试建议"])
        lines.extend(f"- {_safe_text(item)}" for item in result.test_suggestions)

    lines.extend(["", "> 本评论由 DeepSeek 生成，仅供人工 Review 参考。"])
    return _bounded_text("\n".join(lines), max_bytes)


def _render_finding(finding: ReviewFinding) -> list[str]:
    location = f"`{_safe_text(finding.path)}:{finding.line}`"
    if finding.end_line and finding.end_line != finding.line:
        location = f"`{_safe_text(finding.path)}:{finding.line}-{finding.end_line}`"
    return [
        "",
        f"#### {finding.priority} · {location}",
        f"**{_safe_text(finding.title)}**",
        "",
        f"**问题：** {_safe_text(finding.problem)}",
        "",
        f"**影响：** {_safe_text(finding.impact)}",
        "",
        f"**建议：** {_safe_text(finding.suggestion)}",
        "",
        f"置信度：{finding.confidence:.0%}",
    ]


def _safe_text(value: str) -> str:
    cleaned = value.replace("\x00", "").strip()
    return html.escape(redact_sensitive_text(cleaned), quote=False)


def _bounded_text(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    suffix = "\n\n（审查评论因长度限制已截断。）"
    suffix_encoded = suffix.encode("utf-8")
    if max_bytes <= len(suffix_encoded):
        return suffix_encoded[:max_bytes].decode("utf-8", errors="ignore")
    available = max_bytes - len(suffix_encoded)
    return encoded[:available].decode("utf-8", errors="ignore") + suffix
