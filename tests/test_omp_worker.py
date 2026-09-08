import io
from zipfile import ZipFile

import pytest

from reviewbot.omp_worker import _extract_archive, _strip_json_fence


def _archive(entries: dict[str, str]) -> bytes:
    payload = io.BytesIO()
    with ZipFile(payload, "w") as bundle:
        for name, content in entries.items():
            bundle.writestr(name, content)
    return payload.getvalue()


def test_extract_archive_returns_single_repository_root(tmp_path) -> None:
    root = _extract_archive(_archive({"owner-repo-abc/README.md": "readme"}), tmp_path)

    assert root.name == "owner-repo-abc"
    assert (root / "README.md").read_text(encoding="utf-8") == "readme"


def test_extract_archive_rejects_path_traversal(tmp_path) -> None:
    with pytest.raises(ValueError, match="unsafe path"):
        _extract_archive(_archive({"../escape.txt": "blocked"}), tmp_path)


def test_strip_json_fence_accepts_json_markdown() -> None:
    assert _strip_json_fence("```json\n{\"verdict\": \"clean\"}\n```") == '{"verdict": "clean"}'
