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

    def has_changed_line(self, path: str, line: int) -> bool:
        return line in self.changed_lines.get(path, frozenset())


def build_diff_context(files: list[ChangedFile], max_bytes: int) -> DiffContext:
    sections: list[str] = []
    changed_lines: dict[str, frozenset[int]] = {}
    omitted: list[str] = []
    used = 0

    for changed_file in files:
        lines = parse_changed_lines(changed_file.patch)
        changed_lines[changed_file.filename] = frozenset(lines)
        if not changed_file.patch:
            omitted.append(changed_file.filename)
            continue
        section = _render_file(changed_file)
        encoded_size = len(section.encode("utf-8"))
        if used + encoded_size > max_bytes:
            omitted.append(changed_file.filename)
            continue
        sections.append(section)
        used += encoded_size

    if omitted:
        sections.append(
            "\n[The following changed files were omitted because the diff exceeded "
            "the review limit; do not invent findings for them.]\n" + "\n".join(f"- {path}" for path in omitted)
        )

    return DiffContext(text="\n".join(sections), changed_lines=changed_lines, omitted_files=tuple(omitted))


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
