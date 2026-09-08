from pathlib import Path

from reviewbot.rules import compose_rules
from reviewbot.service import load_rules


def test_compose_rules_matches_global_repository_and_changed_paths(tmp_path: Path) -> None:
    policy = tmp_path / "review-rules.toml"
    policy.write_text(
        """
rules = "global policy"

[paths."src/auth/**"]
rules = ["check authorization", "check token handling"]

[repositories."Owner/Repo"]
rules = "preserve API compatibility"

[repositories."Owner/Repo".paths."tests/**"]
rules = "add a regression test"
""".strip(),
        encoding="utf-8",
    )

    result = compose_rules(
        "base rules",
        policy_file=policy,
        repository="owner/repo",
        changed_paths=("src\\auth\\login.py", "tests/test_login.py"),
    )

    assert result.startswith("base rules")
    assert "global policy" in result
    assert "check authorization" in result
    assert "preserve API compatibility" in result
    assert "add a regression test" in result


def test_compose_rules_ignores_unmatched_paths_and_invalid_policy(tmp_path: Path) -> None:
    policy = tmp_path / "rules.toml"
    policy.write_text(
        """
[paths."src/auth/**"]
rules = "auth-only"
""".strip(),
        encoding="utf-8",
    )

    assert compose_rules(
        "base",
        policy_file=policy,
        repository="owner/repo",
        changed_paths=("docs/readme.md",),
    ) == "base"

    policy.write_text("not = [valid", encoding="utf-8")
    assert compose_rules(
        "base",
        policy_file=policy,
        repository="owner/repo",
        changed_paths=("src/auth/login.py",),
    ) == "base"


def test_path_rules_respect_directory_and_case_boundaries(tmp_path: Path) -> None:
    policy = tmp_path / "rules.toml"
    policy.write_text(
        """
[paths."src/*.py"]
rules = "direct child only"

[paths."src/Auth/**"]
rules = "case-sensitive auth"
""".strip(),
        encoding="utf-8",
    )

    nested = compose_rules(
        "base",
        policy_file=policy,
        repository="owner/repo",
        changed_paths=("src/pkg/module.py", "src/auth/token.py"),
    )
    assert "direct child only" not in nested
    assert "case-sensitive auth" not in nested

    direct = compose_rules(
        "base",
        policy_file=policy,
        repository="owner/repo",
        changed_paths=("src/main.py", "src/Auth/token.py"),
    )
    assert "direct child only" in direct
    assert "case-sensitive auth" in direct


def test_invalid_policy_encoding_falls_back_to_base(tmp_path: Path) -> None:
    policy = tmp_path / "rules.toml"
    policy.write_bytes(b"rules = '" + bytes([0xFF]) + b"'")
    assert compose_rules(
        "base",
        policy_file=policy,
        repository="owner/repo",
        changed_paths=("src/main.py",),
    ) == "base"


def test_load_rules_keeps_default_when_global_file_missing(tmp_path: Path) -> None:
    result = load_rules(tmp_path / "missing.md")
    assert result.startswith("# Review rules")
