"""
job_runs audit logging (DESIGN.md §6.7, §4).

Every stage — cron or manual — writes through the same logging path so
`run_id` can always be traced back to what triggered it. This wraps that
pattern in a context manager so ingestion (and later, every other stage)
doesn't reimplement start/end/status/error bookkeeping.

Usage:
    with JobRun(conn, stage="ingestion_price", run_type="cron") as job:
        ... do work ...
        job.note("processed 400 tickers")
    # commits a 'complete' row on clean exit, 'failed' + error_message on exception
"""

import os
import uuid
from datetime import datetime, timezone


class JobRun:
    def __init__(self, conn, stage: str, run_type: str = "cron", triggered_by: str = None):
        assert run_type in ("cron", "manual")
        self.conn = conn
        self.stage = stage
        self.run_type = run_type
        self.triggered_by = triggered_by or os.environ.get("GITHUB_ACTOR", "local")
        self.run_id = f"{stage}-{uuid.uuid4().hex[:10]}"
        self.start_time = None
        self._notes = []

    def note(self, text: str):
        self._notes.append(text)

    def __enter__(self):
        self.start_time = datetime.now(timezone.utc)
        with self.conn:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO job_runs (run_id, stage, run_type, triggered_by, start_time, status)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (self.run_id, self.stage, self.run_type, self.triggered_by, self.start_time, "running"),
                )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        end_time = datetime.now(timezone.utc)
        status = "failed" if exc_type else "complete"
        error_message = None
        if exc_type:
            error_message = f"{exc_type.__name__}: {exc_val}"
        if self._notes:
            joined = " | ".join(self._notes)
            error_message = f"{error_message} | notes: {joined}" if error_message else f"notes: {joined}"

        # Defensive: if the exception we're logging left the connection's
        # transaction in a failed state (any earlier statement that wasn't
        # itself wrapped in `with conn:`), the UPDATE below would raise
        # InFailedSqlTransaction and that NEW exception would replace the
        # real one in what the caller sees — silently turning a specific,
        # diagnosable error into a generic "transaction is aborted"
        # message. Roll back first, unconditionally, so logging the
        # failure can never itself mask the failure.
        try:
            self.conn.rollback()
        except Exception:
            pass

        with self.conn:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE job_runs
                    SET end_time = %s, status = %s, error_message = %s
                    WHERE run_id = %s
                    """,
                    (end_time, status, error_message, self.run_id),
                )
        # Never swallow the exception — let the caller's script exit non-zero.
        return False
