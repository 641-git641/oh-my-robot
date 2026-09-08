import hashlib
import hmac
import json

from reviewbot.diff import build_diff_context, parse_changed_lines
from reviewbot.models import ChangedFile
from reviewbot.webhook import is_pull_request_event, is_review_action, parse_review_job, verify_webhook


def test_github_webhook_parses_pull_request_payload_and_verifies_hmac() -> None:
    payload = {
        "action": "opened",
        "number": 12,
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 12,
            "head": {"sha": "head-12"},
        },
    }
    body = json.dumps(payload).encode()
    digest = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()

    assert verify_webhook(
        body,
        secret="webhook-secret",
        signature_header=f"sha256={digest}",
    )
    job = parse_review_job(body=body, event_type="pull_request", delivery_id="delivery-12")

    assert job.repository == "owner/repo"
    assert job.pull_request_number == 12
    assert job.action == "opened"
    assert job.webhook_head_sha == "head-12"
    assert is_pull_request_event("pull_request", payload)
    assert is_review_action(job.action)


def test_webhook_rejects_missing_or_unprefixed_signature() -> None:
    body = b'{"number":1}'

    assert not verify_webhook(body, secret="secret", signature_header=None)
    assert not verify_webhook(body, secret="secret", signature_header="secret")


def test_diff_context_tracks_added_lines_and_omits_large_files() -> None:
    patch = """@@ -1,3 +1,4 @@\n line one\n+line two\n-line three\n+line four\n\\ No newline at end of file\n"""
    assert parse_changed_lines(patch) == {2, 3}

    files = [ChangedFile(filename="src/example.ts", patch=patch, additions=2, deletions=1)]
    context = build_diff_context(files, max_bytes=10_000)
    assert context.has_changed_line("src/example.ts", 2)
    assert not context.has_changed_line("src/example.ts", 4)
    assert "src/example.ts" in context.text

    omitted = build_diff_context(files, max_bytes=1)
    assert omitted.omitted_files == ("src/example.ts",)
    assert "omitted" in omitted.text


def test_diff_context_marks_patchless_files_unavailable() -> None:
    context = build_diff_context(
        [ChangedFile(filename="assets/logo.png", patch="", additions=1)],
        max_bytes=10_000,
    )

    assert context.omitted_files == ("assets/logo.png",)
    assert "diff -- assets/logo.png" not in context.text
