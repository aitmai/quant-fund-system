"""
Phase 0 milestone script (DESIGN.md §13).

Proves the full chain works: this script runs (locally, or via the
GitHub Actions workflow in .github/workflows/hello_world.yml), connects
to Supabase, and writes one row to job_runs. That's the entire Phase 0
milestone — nothing pipeline-specific yet.

Usage:
    pip install -r requirements.txt
    export DATABASE_URL="postgresql://..."   # from Supabase project settings
    python scripts/test_connection.py
"""

import os
import sys
import uuid
from datetime import datetime, timezone

import psycopg2


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL is not set. See .env.example.", file=sys.stderr)
        return 1

    run_id = f"phase0-hello-world-{uuid.uuid4().hex[:8]}"
    start_time = datetime.now(timezone.utc)

    try:
        conn = psycopg2.connect(database_url)
    except Exception as exc:
        print(f"ERROR: could not connect to Supabase: {exc}", file=sys.stderr)
        return 1

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO job_runs (run_id, stage, run_type, triggered_by,
                                           start_time, end_time, status, error_message)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        "phase0_scaffolding",
                        "manual",
                        os.environ.get("GITHUB_ACTOR", "local"),
                        start_time,
                        datetime.now(timezone.utc),
                        "complete",
                        None,
                    ),
                )
        print(f"OK — wrote job_runs row {run_id}. Supabase connection confirmed.")
        return 0
    except Exception as exc:
        print(f"ERROR: connected, but insert failed: {exc}", file=sys.stderr)
        print(
            "Did you run migrations/001_initial_schema.sql against this database yet?",
            file=sys.stderr,
        )
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
