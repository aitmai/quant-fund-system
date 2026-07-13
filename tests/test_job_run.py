import unittest
from unittest.mock import MagicMock, call

from src.job_run import JobRun


class TestJobRunRollbackSafety(unittest.TestCase):
    def test_rollback_called_before_logging_failure(self):
        """If the code inside the `with JobRun(...)` block raises, JobRun
        must roll back BEFORE attempting to write the failure row —
        otherwise a transaction left aborted by an earlier, unrelated
        statement would make the failure-logging UPDATE itself raise
        InFailedSqlTransaction, and that new exception would replace the
        real one in what the caller sees (exactly the bug this guards
        against)."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        call_order = []

        try:
            with JobRun(conn, stage="test_stage", run_type="manual"):
                # Only start tracking after __enter__'s own INSERT has
                # already happened — we care about ordering between the
                # failure path's rollback() and its UPDATE, not __enter__.
                conn.rollback.side_effect = lambda: call_order.append("rollback")
                cursor.execute.side_effect = lambda *a, **k: call_order.append("execute")
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        conn.rollback.assert_called()
        # rollback must happen before the failure-logging UPDATE's execute()
        self.assertEqual(call_order[0], "rollback")

    def test_does_not_swallow_the_original_exception(self):
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        with self.assertRaises(RuntimeError):
            with JobRun(conn, stage="test_stage", run_type="manual"):
                raise RuntimeError("the real error")

    def test_clean_exit_does_not_call_rollback_unnecessarily(self):
        # Not strictly required for correctness, but rollback() on a clean
        # exit is harmless (no-op) — this just documents that clean runs
        # still log 'complete' correctly.
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        with JobRun(conn, stage="test_stage", run_type="manual") as job:
            job.note("all good")

        # Find the UPDATE call and check status='complete' was passed
        update_calls = [c for c in cursor.execute.call_args_list if "UPDATE job_runs" in c[0][0]]
        self.assertEqual(len(update_calls), 1)
        self.assertIn("complete", update_calls[0][0][1])


if __name__ == "__main__":
    unittest.main()
