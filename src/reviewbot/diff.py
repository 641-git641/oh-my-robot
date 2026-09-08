from __future__ import annotations

import re
from dataclasses import dataclass

from reviewbot.models import ChangedFile

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True, slots=True)
class DiffContext:
    text: str
    changed_lines: dict[str, frozenset[int]]
    omitted_files: tuple[str, ...]
    files: tuple[str, ...] = ()

    def has_changed_line(self, path: str, line: int) -> bool:
        return line in self.changed_lines.get(path, frozenset())


@dataclass(frozen=True, slots=True)
class DiffReviewPlan:
    batches: tuple[DiffContext, ...]
    total_files: int
    reviewed_files: tuple[str, ...]
    omitted_files: tuple[str, ...]
    patchless_files: tuple[str, ...]
    total_bytes: int

    @property
    def batch_count(self) -> int:
        return len(self.batches)


def build_diff_batches(files: list[ChangedFile], max_bytes: int) -> DiffReviewPlan:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    all_changed_lines: dict[str, frozenset[int]] = {}
    batches: list[DiffContext] = []
    sections: list[str] = []
    batch_files: list[str] = []
    reviewed: list[str] = []
    omitted: list[str] = []
    patchless: list[str] = []
    used = 0
    total_bytes = 0

    def flush() -> None:
        nonlocal sections, batch_files, used
        if not sections:
            return
        batches.append(
            DiffContext(
                text="\n".join(sections),
                changed_lines={
                    path: all_changed_lines[path]
                    for path in batch_files
                },
                omitted_files=(),
                files=tuple(batch_files),
            )
        )
        sections = []
        batch_files = []
        used = 0

    for changed_file in files:
        lines = frozenset(parse_changed_lines(changed_file.patch))
        all_changed_lines[changed_file.filename] = lines
        if not changed_file.patch:
            omitted.append(changed_file.filename)
            patchless.append(changed_file.filename)
            continue
        section = _render_file(changed_file)
        encoded_size = len(section.encode("utf-8"))
        if encoded_size > max_bytes:
            omitted.append(changed_file.filename)
            continue
        separator_bytes = 1 if sections else 0
        if sections and used + separator_bytes + encoded_size > max_bytes:
            flush()
            separator_bytes = 0
        sections.append(section)
        batch_files.append(changed_file.filename)
        reviewed.append(changed_file.filename)
        used += separator_bytes + encoded_size
        total_bytes += separator_bytes + encoded_size
    flush()

    omitted_tuple = tuple(omitted)
    batches = [
        DiffContext(
            text=batch.text,
            changed_lines=batch.changed_lines,
            omitted_files=omitted_tuple,
            files=batch.files,
        )
        for batch in batches
    ]
    return DiffReviewPlan(
        batches=tuple(batches),
        total_files=len(files),
        reviewed_files=tuple(reviewed),
        omitted_files=omitted_tuple,
        patchless_files=tuple(patchless),
        total_bytes=total_bytes,
    )


def build_diff_context(files: list[ChangedFile], max_bytes: int) -> DiffContext:
    plan = build_diff_batches(files, max_bytes)
    first_batch = plan.batches[0].text if plan.batches else ""
    later_batch_files = tuple(
        path
        for batch in plan.batches[1:]
        for path in batch.files
    )
    legacy_omitted = plan.omitted_files + later_batch_files
    text = _bounded_text(
        f"{first_batch}{_omitted_notice(legacy_omitted)}",
        max_bytes,
    )
    return DiffContext(
        text=text,
        changed_lines={
            changed_file.filename: frozenset(parse_changed_lines(changed_file.patch))
            for changed_file in files
        },
        omitted_files=legacy_omitted,
        files=plan.batches[0].files if plan.batches else (),
    )


def parse_changed_lines(patch: str) -> set[int]:
    changed: set[int] = set()
    new_line: int | None = None
    for raw_line in patch.splitlines():
        match = _HUNK_RE.match(raw_line)
        if match:
            new_line = int(match.group(1))
            continue
        if new_line is None or raw_line.startswith("\\"):
            continue
        if raw_line.startswith("+++"):
            continue
        if raw_line.startswith("+"):
            changed.add(new_line)
            new_line += 1
            continue
        if raw_line.startswith("-"):
            continue
        new_line += 1
    return changed


def _render_file(changed_file: ChangedFile) -> str:
    metadata = (
        f"diff -- {changed_file.filename}\n"
        f"status: {changed_file.status}; additions: {changed_file.additions}; deletions: {changed_file.deletions}\n"
    )
    patch = changed_file.patch or "[No patch was supplied by GitHub for this file.]"
    return f"\n{metadata}{patch}\n"


def _omitted_notice(paths: tuple[str, ...]) -> str:
    if not paths:
        return ""
    return (
        "\n[The following changed files were omitted or unavailable; "
        "do not invent findings for them.]\n"
        + "\n".join(f"- {path}" for path in paths)
    )


def _bounded_text(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")
