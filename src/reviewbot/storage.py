from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from reviewbot.findings import finding_fingerprint
from reviewbot.models import (
    EventRecord,
    EventSource,
    EventState,
    FindingRecord,
    FindingStatus,
    ReviewFinding,
    ReviewJob,
    ReviewRecord,
)

_SCHEMA_VERSION = 1
_EVENT_SOURCES = frozenset({"webhook", "refresh", "replay", "manual"})
_EVENT_COLUMNS = frozenset(
    {
        "delivery_id",
        "event_type",
        "action",
        "repository",
        "pull_request_number",
        "webhook_head_sha",
        "state",
        "attempts",
        "last_error",
        "source",
        "source_delivery_id",
        "available_at",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    }
)


class QueueStore:
    """SQLite persistence for idempotent webhook jobs and review results."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> None:
        with self._connect() as connection:
            if not self._table_exists(connection, "events"):
                self._create_schema(connection)
            elif not _EVENT_COLUMNS.issubset(self._table_columns(connection, "events")):
                self._migrate_events(connection)
            else:
                self._create_schema(connection)
            self._create_supporting_schema(connection)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            connection.execute(
                """
                UPDATE events
                SET state = 'queued',
                    available_at = CURRENT_TIMESTAMP,
                    started_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE state = 'running'
                """
            )

    def enqueue(
        self,
        job: ReviewJob,
        *,
        source: EventSource = "webhook",
        source_delivery_id: str | None = None,
    ) -> bool:
        with self._connect() as connection:
            return self._insert_event(connection, job, source=source, source_delivery_id=source_delivery_id)

    def supersede_and_enqueue(self, delivery_id: str, reason: str, refresh_job: ReviewJob) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE events
                SET state = 'superseded',
                    last_error = ?,
                    finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ? AND state = 'running'
                """,
                (reason, delivery_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"running event not found: {delivery_id}")
            return self._enqueue_refresh_event(connection, refresh_job, source_delivery_id=delivery_id)

    @staticmethod
    def _enqueue_refresh_event(
        connection: sqlite3.Connection,
        refresh_job: ReviewJob,
        *,
        source_delivery_id: str,
    ) -> bool:
        reviewed = connection.execute(
            """
            SELECT 1
            FROM reviews
            WHERE repository = ? AND pull_request_number = ? AND head_sha = ?
            """,
            (
                refresh_job.repository.lower(),
                refresh_job.pull_request_number,
                refresh_job.webhook_head_sha,
            ),
        ).fetchone()
        if reviewed is not None:
            return False
        existing = connection.execute(
            """
            SELECT state
            FROM events
            WHERE delivery_id = ?
            """,
            (refresh_job.delivery_id,),
        ).fetchone()
        if existing is None:
            return QueueStore._insert_event(
                connection,
                refresh_job,
                source="refresh",
                source_delivery_id=source_delivery_id,
            )
        if existing["state"] in {"queued", "running"}:
            return False
        updated = connection.execute(
            """
            UPDATE events
            SET event_type = ?,
                action = ?,
                repository = ?,
                pull_request_number = ?,
                webhook_head_sha = ?,
                state = 'queued',
                attempts = 0,
                last_error = NULL,
                source = 'refresh',
                source_delivery_id = ?,
                available_at = CURRENT_TIMESTAMP,
                started_at = NULL,
                finished_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE delivery_id = ?
            """,
            (
                refresh_job.event_type,
                refresh_job.action,
                refresh_job.repository.lower(),
                refresh_job.pull_request_number,
                refresh_job.webhook_head_sha,
                source_delivery_id,
                refresh_job.delivery_id,
            ),
        )
        return updated.rowcount == 1

    def claim_next(self) -> tuple[ReviewJob, int] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT delivery_id, event_type, action, repository,
                       pull_request_number, webhook_head_sha, attempts
                FROM events AS candidate
                WHERE candidate.state = 'queued'
                  AND candidate.available_at <= CURRENT_TIMESTAMP
                  AND NOT EXISTS (
                      SELECT 1
                      FROM events AS running
                      WHERE running.state = 'running'
                        AND running.repository = candidate.repository
                        AND running.pull_request_number = candidate.pull_request_number
                  )
                ORDER BY candidate.available_at, candidate.created_at, candidate.delivery_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            attempts = int(row["attempts"]) + 1
            connection.execute(
                """
                UPDATE events
                SET state = 'running',
                    attempts = ?,
                    started_at = CURRENT_TIMESTAMP,
                    finished_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ? AND state = 'queued'
                """,
                (attempts, row["delivery_id"]),
            )
            connection.commit()
            return (
                ReviewJob(
                    delivery_id=str(row["delivery_id"]),
                    event_type=str(row["event_type"]),
                    action=str(row["action"]),
                    repository=str(row["repository"]),
                    pull_request_number=int(row["pull_request_number"]),
                    webhook_head_sha=str(row["webhook_head_sha"] or ""),
                ),
                attempts,
            )

    def mark_succeeded(self, delivery_id: str) -> None:
        self._set_event_state(delivery_id, "succeeded", None)

    def mark_skipped(self, delivery_id: str, reason: str) -> None:
        self._set_event_state(delivery_id, "skipped", reason)

    def mark_superseded(self, delivery_id: str, reason: str) -> None:
        self._set_event_state(delivery_id, "superseded", reason)

    def mark_failed(
        self,
        delivery_id: str,
        error: str,
        *,
        retry: bool,
        retry_after: float | None = None,
        error_type: str | None = None,
    ) -> None:
        with self._connect() as connection:
            if retry:
                if retry_after is None:
                    connection.execute(
                        """
                        UPDATE events
                        SET state = 'queued',
                            last_error = ?,
                            error_type = ?,
                            available_at = CURRENT_TIMESTAMP,
                            started_at = NULL,
                            finished_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE delivery_id = ?
                        """,
                        (error, error_type, delivery_id),
                    )
                else:
                    delay = min(max(float(retry_after), 0.0), 3_600.0)
                    connection.execute(
                        """
                        UPDATE events
                        SET state = 'queued',
                            last_error = ?,
                            error_type = ?,
                            available_at = datetime(CURRENT_TIMESTAMP, '+' || ? || ' seconds'),
                            started_at = NULL,
                            finished_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE delivery_id = ?
                        """,
                        (error, error_type, delay, delivery_id),
                    )
            else:
                connection.execute(
                    """
                    UPDATE events
                    SET state = 'failed',
                        last_error = ?,
                        error_type = ?,
                        finished_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE delivery_id = ?
                    """,
                    (error, error_type, delivery_id),
                )


    def requeue_if_running(self, delivery_id: str, error: str, *, error_type: str = "worker") -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE events
                SET state = 'queued',
                    last_error = ?,
                    error_type = ?,
                    available_at = CURRENT_TIMESTAMP,
                    started_at = NULL,
                    finished_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ? AND state = 'running'
                """,
                (error, error_type, delivery_id),
            )
            return cursor.rowcount == 1
    def has_review(self, repository: str, number: int, head_sha: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM reviews WHERE repository = ? AND pull_request_number = ? AND head_sha = ?",
                (repository.lower(), number, head_sha),
            ).fetchone()
            return row is not None

    def record_review(self, repository: str, number: int, head_sha: str, comment_id: int | None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO reviews (
                    repository, pull_request_number, head_sha, comment_id
                ) VALUES (?, ?, ?, ?)
                """,
                (repository.lower(), number, head_sha, comment_id),
            )

    def record_review_metrics(
        self,
        delivery_id: str,
        *,
        diff_file_count: int,
        diff_bytes: int,
        omitted_file_count: int,
        model_input_tokens: int | None,
        model_output_tokens: int | None,
        finding_count: int,
        review_rank: str | None,
        model_duration_ms: float | None,
        github_duration_ms: float | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO review_metrics (
                    delivery_id, diff_file_count, diff_bytes, omitted_file_count,
                    model_input_tokens, model_output_tokens, finding_count,
                    review_rank, model_duration_ms, github_duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    diff_file_count,
                    diff_bytes,
                    omitted_file_count,
                    model_input_tokens,
                    model_output_tokens,
                    finding_count,
                    review_rank,
                    model_duration_ms,
                    github_duration_ms,
                ),
            )

    def record_review_attempt_metrics(
        self,
        delivery_id: str,
        *,
        diff_file_count: int,
        diff_bytes: int,
        omitted_file_count: int,
        model_input_tokens: int | None,
        model_output_tokens: int | None,
        model_duration_ms: float | None,
        github_duration_ms: float | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO review_attempt_metrics (
                    delivery_id, diff_file_count, diff_bytes, omitted_file_count,
                    model_input_tokens, model_output_tokens,
                    model_duration_ms, github_duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    diff_file_count,
                    diff_bytes,
                    omitted_file_count,
                    model_input_tokens,
                    model_output_tokens,
                    model_duration_ms,
                    github_duration_ms,
                ),
            )

    def record_omp_execution(
        self,
        delivery_id: str,
        repository: str,
        number: int,
        head_sha: str,
        session_id: str,
        tool_audit: Sequence[Mapping[str, object]],
    ) -> None:
        normalized_repository = repository.lower()
        bounded_session_id = session_id[:200]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO omp_sessions (
                    delivery_id, repository, pull_request_number, head_sha,
                    session_id, state
                ) VALUES (?, ?, ?, ?, ?, 'completed')
                """,
                (delivery_id, normalized_repository, number, head_sha, bounded_session_id),
            )
            rows: list[tuple[str, str, str, str, int]] = []
            for item in tool_audit:
                event = str(item.get("event", "unknown"))
                if event not in {"start", "end"}:
                    continue
                rows.append(
                    (
                        delivery_id,
                        bounded_session_id,
                        event,
                        str(item.get("tool", "unknown"))[:200],
                        int(bool(item.get("is_error", False))),
                    )
                )
            connection.executemany(
                """
                INSERT INTO omp_tool_audit (
                    delivery_id, session_id, event, tool_name, is_error
                ) VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )

    def complete_review(
        self,
        delivery_id: str,
        repository: str,
        number: int,
        head_sha: str,
        comment_id: int | None,
        *,
        diff_file_count: int,
        diff_bytes: int,
        omitted_file_count: int,
        model_input_tokens: int | None,
        model_output_tokens: int | None,
        finding_count: int,
        review_rank: str | None,
        model_duration_ms: float | None,
        github_duration_ms: float | None,
        findings: Sequence[ReviewFinding] | None = None,
        reviewed_paths: frozenset[str] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if findings is not None:
                self._sync_findings(
                    connection,
                    repository=repository,
                    number=number,
                    head_sha=head_sha,
                    findings=findings,
                    reviewed_paths=reviewed_paths,
                )
            connection.execute(
                """
                INSERT OR REPLACE INTO reviews (
                    repository, pull_request_number, head_sha, comment_id
                ) VALUES (?, ?, ?, ?)
                """,
                (repository.lower(), number, head_sha, comment_id),
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO review_metrics (
                    delivery_id, diff_file_count, diff_bytes, omitted_file_count,
                    model_input_tokens, model_output_tokens, finding_count,
                    review_rank, model_duration_ms, github_duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    diff_file_count,
                    diff_bytes,
                    omitted_file_count,
                    model_input_tokens,
                    model_output_tokens,
                    finding_count,
                    review_rank,
                    model_duration_ms,
                    github_duration_ms,
                ),
            )
            connection.execute(
                """
                UPDATE events
                SET state = 'succeeded',
                    last_error = NULL,
                    error_type = NULL,
                    finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ?
                """,
                (delivery_id,),
            )

    def metrics(self) -> dict[str, object]:
        with self._connect() as connection:
            state_rows = connection.execute(
                "SELECT state, COUNT(*) FROM events GROUP BY state"
            ).fetchall()
            counts = {str(state): int(count) for state, count in state_rows}
            totals = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(MAX(attempts - 1, 0)), 0)
                FROM events
                """
            ).fetchone()
            oldest = connection.execute(
                """
                SELECT MAX(0, (julianday('now') - julianday(MIN(available_at))) * 86400)
                FROM events
                WHERE state = 'queued'
                """
            ).fetchone()[0]
            review_totals = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(diff_file_count), 0),
                       COALESCE(SUM(diff_bytes), 0),
                       COALESCE(SUM(omitted_file_count), 0),
                       COALESCE(SUM(finding_count), 0),
                       COALESCE(SUM(model_input_tokens), 0),
                       COALESCE(SUM(model_output_tokens), 0),
                       COALESCE(SUM(model_duration_ms), 0),
                       COALESCE(SUM(github_duration_ms), 0),
                       COALESCE(AVG(model_duration_ms), 0),
                       COALESCE(AVG(github_duration_ms), 0),
                       COALESCE(MAX(model_duration_ms), 0),
                       COALESCE(MAX(github_duration_ms), 0)
                FROM review_metrics
                """
            ).fetchone()
            attempt_totals = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(model_input_tokens), 0),
                       COALESCE(SUM(model_output_tokens), 0),
                       COALESCE(SUM(model_duration_ms), 0)
                FROM review_attempt_metrics
                """
            ).fetchone()
            rank_rows = connection.execute(
                """
                SELECT COALESCE(review_rank, 'unknown'), COUNT(*)
                FROM review_metrics
                GROUP BY COALESCE(review_rank, 'unknown')
                ORDER BY COALESCE(review_rank, 'unknown')
                """
            ).fetchall()
            error_rows = connection.execute(
                """
                SELECT error_type, COUNT(*)
                FROM events
                WHERE error_type IS NOT NULL
                GROUP BY error_type
                ORDER BY error_type
                """
            ).fetchall()
            attempt_count = int(attempt_totals[0])
            model_input_tokens = int(attempt_totals[1]) if attempt_count else int(review_totals[5])
            model_output_tokens = int(attempt_totals[2]) if attempt_count else int(review_totals[6])
            model_duration_total = float(attempt_totals[3]) if attempt_count else float(review_totals[7])
            return {
                "queue": counts,
                "eventsTotal": int(totals[0]),
                "retryCount": int(totals[1]),
                "errorCounts": {str(error_type): int(count) for error_type, count in error_rows},
                "running": counts.get("running", 0),
                "reviewsTotal": int(connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]),
                "oldestQueuedAgeSeconds": float(oldest) if oldest is not None else None,
                "reviewMetrics": {
                    "reviewCount": int(review_totals[0]),
                    "attemptCount": attempt_count,
                    "diffFileCount": int(review_totals[1]),
                    "diffBytes": int(review_totals[2]),
                    "omittedFileCount": int(review_totals[3]),
                    "findingCount": int(review_totals[4]),
                    "modelInputTokens": model_input_tokens,
                    "modelOutputTokens": model_output_tokens,
                    "modelDurationMs": {
                        "total": model_duration_total,
                        "averageCompleted": float(review_totals[9]),
                        "maxCompleted": float(review_totals[11]),
                    },
                    "githubDurationMs": {
                        "totalCompleted": float(review_totals[8]),
                        "averageCompleted": float(review_totals[10]),
                        "maxCompleted": float(review_totals[12]),
                    },
                    "reviewRanks": {str(rank): int(count) for rank, count in rank_rows},
                },
            }

    def get_event(self, delivery_id: str) -> EventRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM events WHERE delivery_id = ?", (delivery_id,)).fetchone()
            return self._event_from_row(row) if row else None

    def list_events(
        self,
        *,
        state: EventState | None = None,
        repository: str | None = None,
        pull_request_number: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EventRecord]:
        limit, offset = _page_values(limit, offset)
        clauses = ["1 = 1"]
        parameters: list[object] = []
        if state is not None:
            clauses.append("state = ?")
            parameters.append(state)
        if repository is not None:
            clauses.append("repository = ?")
            parameters.append(repository.lower())
        if pull_request_number is not None:
            clauses.append("pull_request_number = ?")
            parameters.append(pull_request_number)
        parameters.extend((limit, offset))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM events
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, delivery_id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
            return [self._event_from_row(row) for row in rows]

    def list_reviews(
        self,
        *,
        repository: str | None = None,
        pull_request_number: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReviewRecord]:
        limit, offset = _page_values(limit, offset)
        clauses = ["1 = 1"]
        parameters: list[object] = []
        if repository is not None:
            clauses.append("repository = ?")
            parameters.append(repository.lower())
        if pull_request_number is not None:
            clauses.append("pull_request_number = ?")
            parameters.append(pull_request_number)
        parameters.extend((limit, offset))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM reviews
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, repository, pull_request_number, head_sha
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
            return [self._review_from_row(row) for row in rows]

    def list_findings(
        self,
        *,
        repository: str | None = None,
        pull_request_number: int | None = None,
        status: FindingStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[FindingRecord]:
        limit, offset = _page_values(limit, offset)
        clauses = ["1 = 1"]
        parameters: list[object] = []
        if repository is not None:
            clauses.append("repository = ?")
            parameters.append(repository.lower())
        if pull_request_number is not None:
            clauses.append("pull_request_number = ?")
            parameters.append(pull_request_number)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        parameters.extend((limit, offset))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM findings
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, fingerprint
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
            return [self._finding_from_row(row) for row in rows]

    def event_state(self, delivery_id: str) -> str | None:
        event = self.get_event(delivery_id)
        return event.state if event else None

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows: Iterable[tuple[str, int]] = connection.execute(
                "SELECT state, COUNT(*) FROM events GROUP BY state"
            ).fetchall()
            return {str(state): int(count) for state, count in rows}

    @staticmethod
    def _sync_findings(
        connection: sqlite3.Connection,
        *,
        repository: str,
        number: int,
        head_sha: str,
        findings: Sequence[ReviewFinding],
        reviewed_paths: frozenset[str] | None,
    ) -> None:
        normalized_repository = repository.lower()
        existing_rows = connection.execute(
            """
            SELECT *
            FROM findings
            WHERE repository = ? AND pull_request_number = ?
            """,
            (normalized_repository, number),
        ).fetchall()
        existing = {str(row["fingerprint"]): row for row in existing_rows}
        seen: set[str] = set()
        normalized_reviewed_paths = (
            None
            if reviewed_paths is None
            else {path.replace("\\", "/") for path in reviewed_paths}
        )
        for finding in findings:
            fingerprint = finding_fingerprint(finding)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            previous = existing.get(fingerprint)
            if previous is None:
                status = "new"
                first_seen_sha = head_sha
            else:
                same_location = (
                    str(previous["path"]) == finding.path.replace("\\", "/")
                    and int(previous["line"]) == finding.line
                    and previous["end_line"] == finding.end_line
                )
                status = "active" if same_location else "relocated"
                first_seen_sha = str(previous["first_seen_sha"])
            connection.execute(
                """
                INSERT INTO findings (
                    repository, pull_request_number, fingerprint, head_sha,
                    path, line, end_line, priority, title, status,
                    first_seen_sha, last_seen_sha
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository, pull_request_number, fingerprint) DO UPDATE SET
                    head_sha = excluded.head_sha,
                    path = excluded.path,
                    line = excluded.line,
                    end_line = excluded.end_line,
                    priority = excluded.priority,
                    title = excluded.title,
                    status = excluded.status,
                    first_seen_sha = excluded.first_seen_sha,
                    last_seen_sha = excluded.last_seen_sha,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    normalized_repository,
                    number,
                    fingerprint,
                    head_sha,
                    finding.path.replace("\\", "/"),
                    finding.line,
                    finding.end_line,
                    finding.priority,
                    finding.title,
                    status,
                    first_seen_sha,
                    head_sha,
                ),
            )
        for fingerprint, previous in existing.items():
            if (
                fingerprint not in seen
                and str(previous["status"]) != "resolved"
                and (
                    normalized_reviewed_paths is None
                    or str(previous["path"]) in normalized_reviewed_paths
                )
            ):
                connection.execute(
                    """
                    UPDATE findings
                    SET status = 'resolved',
                        head_sha = ?,
                        last_seen_sha = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE repository = ? AND pull_request_number = ? AND fingerprint = ?
                    """,
                    (head_sha, head_sha, normalized_repository, number, fingerprint),
                )

    def _set_event_state(self, delivery_id: str, state: EventState, error: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE events
                SET state = ?, last_error = ?, error_type = NULL, finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ?
                """,
                (state, error, delivery_id),
            )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        job: ReviewJob,
        *,
        source: EventSource,
        source_delivery_id: str | None,
    ) -> bool:
        if source not in _EVENT_SOURCES:
            raise ValueError(f"unsupported event source: {source!r}")
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO events (
                delivery_id, event_type, action, repository, pull_request_number,
                webhook_head_sha, state, source, source_delivery_id
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                job.delivery_id,
                job.event_type,
                job.action,
                job.repository.lower(),
                job.pull_request_number,
                job.webhook_head_sha,
                source,
                source_delivery_id,
            ),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                delivery_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                action TEXT NOT NULL,
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                webhook_head_sha TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('queued', 'running', 'succeeded', 'failed', 'skipped', 'superseded')
                ),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                error_type TEXT,
                source TEXT NOT NULL DEFAULT 'webhook' CHECK (
                    source IN ('webhook', 'refresh', 'replay', 'manual')
                ),
                source_delivery_id TEXT,
                available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_events_state_available_created
                ON events (state, available_at, created_at, delivery_id);
            CREATE INDEX IF NOT EXISTS idx_events_repository_pr_state
                ON events (repository, pull_request_number, state);
            CREATE TABLE IF NOT EXISTS reviews (
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                comment_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (repository, pull_request_number, head_sha)
            );
            CREATE TABLE IF NOT EXISTS findings (
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                path TEXT NOT NULL,
                line INTEGER NOT NULL,
                end_line INTEGER,
                priority TEXT NOT NULL CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
                title TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('new', 'active', 'resolved', 'relocated')
                ),
                first_seen_sha TEXT NOT NULL,
                last_seen_sha TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (repository, pull_request_number, fingerprint)
            );
            CREATE INDEX IF NOT EXISTS idx_findings_repository_pr_status
                ON findings (repository, pull_request_number, status);
            CREATE TABLE IF NOT EXISTS omp_sessions (
                delivery_id TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                session_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('completed', 'failed')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS omp_tool_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                event TEXT NOT NULL CHECK (event IN ('start', 'end')),
                tool_name TEXT NOT NULL,
                is_error INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_omp_tool_audit_delivery
                ON omp_tool_audit (delivery_id, id);
            CREATE TABLE IF NOT EXISTS review_metrics (
                delivery_id TEXT PRIMARY KEY,
                diff_file_count INTEGER NOT NULL DEFAULT 0,
                diff_bytes INTEGER NOT NULL DEFAULT 0,
                omitted_file_count INTEGER NOT NULL DEFAULT 0,
                model_input_tokens INTEGER,
                model_output_tokens INTEGER,
                finding_count INTEGER NOT NULL DEFAULT 0,
                review_rank TEXT,
                model_duration_ms REAL,
                github_duration_ms REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS review_attempt_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL,
                diff_file_count INTEGER NOT NULL DEFAULT 0,
                diff_bytes INTEGER NOT NULL DEFAULT 0,
                omitted_file_count INTEGER NOT NULL DEFAULT 0,
                model_input_tokens INTEGER,
                model_output_tokens INTEGER,
                model_duration_ms REAL,
                github_duration_ms REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    @staticmethod
    def _create_supporting_schema(connection: sqlite3.Connection) -> None:
        if "error_type" not in QueueStore._table_columns(connection, "events"):
            connection.execute("ALTER TABLE events ADD COLUMN error_type TEXT")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_events_state_available_created
                ON events (state, available_at, created_at, delivery_id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_events_repository_pr_state
                ON events (repository, pull_request_number, state)
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS review_metrics (
                delivery_id TEXT PRIMARY KEY,
                diff_file_count INTEGER NOT NULL DEFAULT 0,
                diff_bytes INTEGER NOT NULL DEFAULT 0,
                omitted_file_count INTEGER NOT NULL DEFAULT 0,
                model_input_tokens INTEGER,
                model_output_tokens INTEGER,
                finding_count INTEGER NOT NULL DEFAULT 0,
                review_rank TEXT,
                model_duration_ms REAL,
                github_duration_ms REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS review_attempt_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL,
                diff_file_count INTEGER NOT NULL DEFAULT 0,
                diff_bytes INTEGER NOT NULL DEFAULT 0,
                omitted_file_count INTEGER NOT NULL DEFAULT 0,
                model_input_tokens INTEGER,
                model_output_tokens INTEGER,
                model_duration_ms REAL,
                github_duration_ms REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                comment_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (repository, pull_request_number, head_sha)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS findings (
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                path TEXT NOT NULL,
                line INTEGER NOT NULL,
                end_line INTEGER,
                priority TEXT NOT NULL CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
                title TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('new', 'active', 'resolved', 'relocated')
                ),
                first_seen_sha TEXT NOT NULL,
                last_seen_sha TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (repository, pull_request_number, fingerprint)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_findings_repository_pr_status
                ON findings (repository, pull_request_number, status)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS omp_sessions (
                delivery_id TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                session_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('completed', 'failed')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS omp_tool_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                event TEXT NOT NULL CHECK (event IN ('start', 'end')),
                tool_name TEXT NOT NULL,
                is_error INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_omp_tool_audit_delivery
                ON omp_tool_audit (delivery_id, id)
            """
        )

    @staticmethod
    def _migrate_events(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TABLE IF EXISTS events_v2")
        connection.execute(
            """
            CREATE TABLE events_v2 (
                delivery_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                action TEXT NOT NULL,
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                webhook_head_sha TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('queued', 'running', 'succeeded', 'failed', 'skipped', 'superseded')
                ),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                error_type TEXT,
                source TEXT NOT NULL DEFAULT 'webhook' CHECK (
                    source IN ('webhook', 'refresh', 'replay', 'manual')
                ),
                source_delivery_id TEXT,
                available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO events_v2 (
                delivery_id, event_type, action, repository, pull_request_number,
                webhook_head_sha, state, attempts, last_error, error_type, source,
                source_delivery_id, available_at, started_at, finished_at,
                created_at, updated_at
            )
            SELECT delivery_id, event_type, action, repository, pull_request_number,
                   webhook_head_sha, state, attempts, last_error, NULL, 'webhook',
                   NULL, COALESCE(updated_at, created_at, CURRENT_TIMESTAMP),
                   NULL, NULL, created_at, updated_at
            FROM events
            """
        )
        old_count = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        new_count = int(connection.execute("SELECT COUNT(*) FROM events_v2").fetchone()[0])
        if old_count != new_count:
            raise RuntimeError("events migration row count mismatch")
        connection.execute("DROP TABLE events")
        connection.execute("ALTER TABLE events_v2 RENAME TO events")

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return frozenset(str(row["name"]) for row in rows)

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            delivery_id=str(row["delivery_id"]),
            event_type=str(row["event_type"]),
            action=str(row["action"]),
            repository=str(row["repository"]),
            pull_request_number=int(row["pull_request_number"]),
            webhook_head_sha=str(row["webhook_head_sha"] or ""),
            state=str(row["state"]),
            attempts=int(row["attempts"]),
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
            error_type=str(row["error_type"]) if row["error_type"] is not None else None,
            source=str(row["source"]),
            source_delivery_id=(
                str(row["source_delivery_id"]) if row["source_delivery_id"] is not None else None
            ),
            available_at=str(row["available_at"]),
            started_at=str(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _review_from_row(row: sqlite3.Row) -> ReviewRecord:
        return ReviewRecord(
            repository=str(row["repository"]),
            pull_request_number=int(row["pull_request_number"]),
            head_sha=str(row["head_sha"]),
            comment_id=int(row["comment_id"]) if row["comment_id"] is not None else None,
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _finding_from_row(row: sqlite3.Row) -> FindingRecord:
        return FindingRecord(
            fingerprint=str(row["fingerprint"]),
            repository=str(row["repository"]),
            pull_request_number=int(row["pull_request_number"]),
            head_sha=str(row["head_sha"]),
            path=str(row["path"]),
            line=int(row["line"]),
            end_line=int(row["end_line"]) if row["end_line"] is not None else None,
            priority=str(row["priority"]),
            title=str(row["title"]),
            status=str(row["status"]),
            first_seen_sha=str(row["first_seen_sha"]),
            last_seen_sha=str(row["last_seen_sha"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _page_values(limit: int, offset: int) -> tuple[int, int]:
    if limit <= 0 or limit > 1_000:
        raise ValueError("limit must be between 1 and 1000")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    return int(limit), int(offset)
