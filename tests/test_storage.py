import sqlite3
from pathlib import Path

import pytest

from reviewbot.models import ReviewFinding, ReviewJob
from reviewbot.storage import QueueStore


def _job(delivery_id: str, *, repository: str = "owner/repo", number: int = 1) -> ReviewJob:
    return ReviewJob(
        delivery_id=delivery_id,
        event_type="pull_request",
        action="opened",
        repository=repository,
        pull_request_number=number,
        webhook_head_sha=f"head-{delivery_id}",
    )


def _create_legacy_database(path: Path, *, invalid_state: bool = False) -> None:
    state_check = "" if invalid_state else " CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'skipped'))"
    state = "invalid" if invalid_state else "running"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            f"""
            CREATE TABLE events (
                delivery_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                action TEXT NOT NULL,
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                webhook_head_sha TEXT NOT NULL,
                state TEXT NOT NULL{state_check},
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE reviews (
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                comment_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (repository, pull_request_number, head_sha)
            );
            INSERT INTO events (
                delivery_id, event_type, action, repository, pull_request_number,
                webhook_head_sha, state, attempts, last_error
            ) VALUES ('legacy-1', 'pull_request', 'opened', 'owner/repo', 1, 'head-1', '{state}', 2, 'old error');
            INSERT INTO reviews (repository, pull_request_number, head_sha, comment_id)
            VALUES ('owner/repo', 1, 'head-old', 7);
            """
        )


def test_initialize_migrates_legacy_schema_without_losing_records(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    _create_legacy_database(database_path)

    store = QueueStore(database_path)
    store.initialize()
    store.initialize()

    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
        state = connection.execute("SELECT state FROM events WHERE delivery_id = 'legacy-1'").fetchone()[0]
        review_count = connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]

    assert version == 1
    assert {
        "source",
        "source_delivery_id",
        "available_at",
        "started_at",
        "finished_at",
    }.issubset(columns)
    assert state == "queued"
    assert review_count == 1
    event = store.get_event("legacy-1")
    assert event is not None
    assert event.attempts == 2
    assert event.last_error == "old error"
    assert event.source == "webhook"

def test_initialize_adds_error_type_to_existing_m4_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "m4.sqlite3"
    store = QueueStore(database_path)
    store.initialize()
    assert store.enqueue(_job("m4-event"))

    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE events_m4 (
                delivery_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                action TEXT NOT NULL,
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                webhook_head_sha TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                source TEXT NOT NULL,
                source_delivery_id TEXT,
                available_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO events_m4 (
                delivery_id, event_type, action, repository, pull_request_number,
                webhook_head_sha, state, attempts, last_error, source,
                source_delivery_id, available_at, started_at, finished_at,
                created_at, updated_at
            )
            SELECT delivery_id, event_type, action, repository, pull_request_number,
                   webhook_head_sha, state, attempts, last_error, source,
                   source_delivery_id, available_at, started_at, finished_at,
                   created_at, updated_at
            FROM events;
            DROP TABLE events;
            ALTER TABLE events_m4 RENAME TO events;
            """
        )

    restarted_store = QueueStore(database_path)
    restarted_store.initialize()
    event = restarted_store.get_event("m4-event")
    assert event is not None
    assert event.error_type is None
    restarted_store.mark_failed("m4-event", "github failure", retry=False, error_type="github")




def test_migration_failure_rolls_back_temporary_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "invalid-legacy.sqlite3"
    _create_legacy_database(database_path, invalid_state=True)

    with pytest.raises(sqlite3.IntegrityError):
        QueueStore(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('events', 'events_v2')"
            )
        }
        state = connection.execute("SELECT state FROM events WHERE delivery_id = 'legacy-1'").fetchone()[0]

    assert tables == {"events"}
    assert state == "invalid"


@pytest.mark.parametrize("source", ["webhook", "refresh", "replay", "manual"])
def test_store_tracks_all_sources_and_states(tmp_path: Path, source: str) -> None:
    store = QueueStore(tmp_path / f"{source}.sqlite3")
    store.initialize()

    source_delivery_id = None if source == "webhook" else "old-1"
    assert store.enqueue(
        _job(f"{source}-1"),
        source=source,  # type: ignore[arg-type]
        source_delivery_id=source_delivery_id,
    )
    event = store.get_event(f"{source}-1")
    assert event is not None
    assert event.source == source
    assert event.source_delivery_id == source_delivery_id

    store.mark_superseded(f"{source}-1", "head changed")
    assert store.event_state(f"{source}-1") == "superseded"
    assert store.list_events(state="superseded")[0].delivery_id == f"{source}-1"


def test_store_queries_have_stable_order_and_pagination(tmp_path: Path) -> None:
    database_path = tmp_path / "robot.sqlite3"
    store = QueueStore(database_path)
    store.initialize()
    for delivery_id in ("a", "b", "c"):
        assert store.enqueue(_job(delivery_id))

    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE events SET created_at = '2026-01-01 00:00:00'")
        connection.commit()

    assert [event.delivery_id for event in store.list_events(limit=3)] == ["c", "b", "a"]
    assert [event.delivery_id for event in store.list_events(limit=1, offset=1)] == ["b"]

    for head_sha, comment_id in (("head-a", 97), ("head-b", 98), ("head-c", 99)):
        store.record_review("owner/repo", 1, head_sha, comment_id)
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE reviews SET created_at = '2026-01-01 00:00:00'")
        connection.commit()

    reviews = store.list_reviews(repository="owner/repo", pull_request_number=1, limit=3)
    assert [review.head_sha for review in reviews] == ["head-a", "head-b", "head-c"]
    assert [review.head_sha for review in store.list_reviews(limit=1, offset=1)] == ["head-b"]


def test_store_rejects_invalid_state_at_database_boundary(tmp_path: Path) -> None:
    database_path = tmp_path / "robot.sqlite3"
    store = QueueStore(database_path)
    store.initialize()
    assert store.enqueue(_job("delivery-1"))

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE events SET state = 'invalid' WHERE delivery_id = 'delivery-1'")


def test_current_schema_running_tasks_recover_on_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "robot.sqlite3"
    store = QueueStore(database_path)
    store.initialize()
    assert store.enqueue(_job("delivery-1"))
    assert store.claim_next() is not None

    restarted_store = QueueStore(database_path)
    restarted_store.initialize()
    event = restarted_store.get_event("delivery-1")
    assert event is not None
    assert event.state == "queued"
    assert event.started_at is None


def test_claim_and_retry_update_execution_timestamps(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "robot.sqlite3")
    store.initialize()
    assert store.enqueue(_job("delivery-1"))

    claimed = store.claim_next()
    assert claimed is not None
    event = store.get_event("delivery-1")
    assert event is not None
    assert claimed[1] == 1
    assert event.state == "running"
    assert event.started_at is not None

    store.mark_failed("delivery-1", "temporary", retry=True)
    event = store.get_event("delivery-1")
    assert event is not None
    assert event.state == "queued"
    assert event.finished_at is None

    claimed_again = store.claim_next()
    assert claimed_again is not None
    assert claimed_again[1] == 2
    store.mark_failed("delivery-1", "permanent", retry=False)
    event = store.get_event("delivery-1")
    assert event is not None
    assert event.state == "failed"
    assert event.finished_at is not None


def test_retry_after_delays_claim_until_available(tmp_path: Path) -> None:
    database_path = tmp_path / "robot.sqlite3"
    store = QueueStore(database_path)
    store.initialize()
    assert store.enqueue(_job("delivery-rate-limit"))
    assert store.claim_next() is not None

    store.mark_failed("delivery-rate-limit", "rate limited", retry=True, retry_after=60)
    assert store.claim_next() is None

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE events SET available_at = CURRENT_TIMESTAMP WHERE delivery_id = ?",
            ("delivery-rate-limit",),
        )
        connection.commit()
    assert store.claim_next() is not None


def test_complete_review_clears_retry_error_category(tmp_path: Path) -> None:
    database_path = tmp_path / "robot.sqlite3"
    store = QueueStore(database_path)
    store.initialize()
    assert store.enqueue(_job("delivery-retry"))
    assert store.claim_next() is not None
    store.mark_failed("delivery-retry", "temporary", retry=True, error_type="github")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE events SET available_at = CURRENT_TIMESTAMP WHERE delivery_id = ?",
            ("delivery-retry",),
        )
        connection.commit()
    assert store.claim_next() is not None

    store.complete_review(
        "delivery-retry",
        "owner/repo",
        1,
        "head-delivery-retry",
        1,
        diff_file_count=1,
        diff_bytes=10,
        omitted_file_count=0,
        model_input_tokens=None,
        model_output_tokens=None,
        finding_count=0,
        review_rank="P0",
        model_duration_ms=1,
        github_duration_ms=1,
    )
    event = store.get_event("delivery-retry")
    assert event is not None
    assert event.error_type is None


def test_finding_lifecycle_tracks_new_active_relocated_and_resolved(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "findings.sqlite3")
    store.initialize()

    def complete(
        delivery_id: str,
        head_sha: str,
        findings: list[ReviewFinding],
        *,
        reviewed_paths: frozenset[str] | None = None,
    ) -> None:
        job = _job(delivery_id)
        job = job.model_copy(update={"webhook_head_sha": head_sha})
        assert store.enqueue(job)
        assert store.claim_next() is not None
        store.complete_review(
            delivery_id,
            "owner/repo",
            1,
            head_sha,
            1,
            diff_file_count=1,
            diff_bytes=10,
            omitted_file_count=0,
            model_input_tokens=None,
            model_output_tokens=None,
            finding_count=len(findings),
            review_rank="P1",
            model_duration_ms=1,
            github_duration_ms=1,
            findings=findings,
            reviewed_paths=reviewed_paths,
        )

    def finding(line: int) -> ReviewFinding:
        return ReviewFinding(
            priority="P1",
            path="src/auth.py",
            line=line,
            symbol="login",
            title="Missing authorization",
            problem="A caller can reach the handler without a permission check.",
            impact="Unauthorized users can access the operation.",
            suggestion="Require the permission before dispatch.",
            confidence=0.95,
        )

    complete("finding-1", "head-1", [finding(10)])
    first = store.list_findings(repository="owner/repo", pull_request_number=1)
    assert len(first) == 1
    assert first[0].status == "new"
    assert first[0].first_seen_sha == "head-1"

    complete("finding-2", "head-2", [], reviewed_paths=frozenset({"src/other.py"}))
    assert store.list_findings(repository="owner/repo", status="new")[0].status == "new"

    complete("finding-3", "head-3", [finding(10)])
    assert store.list_findings(repository="owner/repo", status="active")[0].status == "active"

    complete("finding-4", "head-4", [finding(20)])
    assert store.list_findings(repository="owner/repo", status="relocated")[0].line == 20

    complete("finding-5", "head-5", [])
    resolved = store.list_findings(repository="owner/repo", status="resolved")
    assert len(resolved) == 1
    assert resolved[0].last_seen_sha == "head-5"


def test_store_persists_omp_session_and_safe_tool_audit(tmp_path: Path) -> None:
    database_path = tmp_path / "omp.sqlite3"
    store = QueueStore(database_path)
    store.initialize()
    store.record_omp_execution(
        "omp-delivery",
        "owner/repo",
        1,
        "head-1",
        "session-1",
        (
            {"event": "start", "tool": "read", "tool_call_id": "call-1", "args": "secret"},
            {"event": "end", "tool": "read", "tool_call_id": "call-1", "is_error": False},
            {"event": "ignored", "tool": "write"},
        ),
    )

    with sqlite3.connect(database_path) as connection:
        session = connection.execute(
            "SELECT session_id, state FROM omp_sessions WHERE delivery_id = ?",
            ("omp-delivery",),
        ).fetchone()
        audit = connection.execute(
            "SELECT event, tool_name, is_error FROM omp_tool_audit WHERE delivery_id = ? ORDER BY id",
            ("omp-delivery",),
        ).fetchall()

    assert session == ("session-1", "completed")
    assert audit == [("start", "read", 0), ("end", "read", 0)]
