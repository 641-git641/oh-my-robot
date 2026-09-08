from __future__ import annotations

import hashlib
import json
import re
from typing import Final

from reviewbot.models import ReviewFinding

_WHITESPACE_RE: Final = re.compile(r"\s+")


def finding_fingerprint(finding: ReviewFinding) -> str:
    """Return a stable identity for the same issue in one changed file.

    Line numbers are intentionally excluded so a finding can move within the
    file without becoming a new issue. The path remains part of the identity to
    avoid merging identical wording in different files.
    """
    payload = {
        "path": finding.path.replace("\\", "/"),
        "symbol": _canonical_text(finding.symbol),
        "title": _canonical_text(finding.title),
        "problem": _canonical_text(finding.problem),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value.strip()).casefold()
