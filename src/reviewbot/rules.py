from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def load_rule_text(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return ""


def compose_rules(
    base_rules: str,
    *,
    policy_file: Path | None,
    repository: str,
    changed_paths: Iterable[str],
) -> str:
    """Append trusted repository/path policy blocks to the base prompt.

    The policy file is operator-controlled configuration, never PR content. Its
    optional TOML shape is:

    [repositories."owner/repo"]
    rules = "..."

    [repositories."owner/repo".paths."src/auth/**"]
    rules = "..."

    [paths."tests/**"]
    rules = "..."
    """
    data = _load_policy(policy_file)
    if not data:
        return base_rules

    paths = tuple(_normalize_path(path) for path in changed_paths if path.strip())
    blocks: list[tuple[str, str]] = []
    global_rules = _text_value(data.get("rules"))
    if global_rules:
        blocks.append(("global", global_rules))

    path_rules = data.get("paths")
    if isinstance(path_rules, dict):
        blocks.extend(_matching_blocks(path_rules, paths, prefix="path"))

    repositories = data.get("repositories")
    repository_rules = None
    if isinstance(repositories, dict):
        repository_rules = next(
            (
                value
                for name, value in repositories.items()
                if isinstance(name, str) and name.lower() == repository.lower()
            ),
            None,
        )
    if isinstance(repository_rules, dict):
        repo_text = _text_value(repository_rules.get("rules"))
        if repo_text:
            blocks.append((f"repository:{repository}", repo_text))
        repo_paths = repository_rules.get("paths")
        if isinstance(repo_paths, dict):
            blocks.extend(_matching_blocks(repo_paths, paths, prefix=f"repository:{repository}:path"))

    if not blocks:
        return base_rules
    sections = [base_rules.strip()] if base_rules.strip() else []
    for label, text in _dedupe_blocks(blocks):
        sections.append(f"## Additional review rules ({label})\n\n{text}")
    return "\n\n".join(sections).strip()


def _load_policy(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (FileNotFoundError, OSError, UnicodeError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _matching_blocks(
    patterns: dict[Any, Any],
    paths: tuple[str, ...],
    *,
    prefix: str,
) -> list[tuple[str, str]]:
    matched: list[tuple[str, str]] = []
    for raw_pattern, value in patterns.items():
        if not isinstance(raw_pattern, str) or not isinstance(value, dict):
            continue
        pattern = _normalize_path(raw_pattern)
        if any(_path_matches(path, pattern) for path in paths):
            text = _text_value(value.get("rules"))
            if text:
                matched.append((f"{prefix}:{raw_pattern}", text))
    return matched


def _path_matches(path: str, pattern: str) -> bool:
    regex_parts: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            regex_parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            regex_parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            regex_parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            regex_parts.append("[^/]")
            index += 1
        else:
            regex_parts.append(re.escape(pattern[index]))
            index += 1
    regex_parts.append("$")
    return re.match("".join(regex_parts), path) is not None


def _text_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(f"- {item.strip()}" for item in value if item.strip()).strip()
    return ""


def _normalize_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _dedupe_blocks(blocks: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for label, text in blocks:
        key = text.strip()
        if key and key not in seen:
            seen.add(key)
            result.append((label, key))
    return result
