"""
Shared Postgres connection helper.

Deliberately plain psycopg2 (no ORM) to match the pattern already
established in scripts/test_connection.py from Phase 0 — one
connection style throughout the codebase.
"""

import os
import sys

import psycopg2


def get_connection():
    """Connect to Supabase Postgres using DATABASE_URL from the environment.

    Raises SystemExit with a clear message if DATABASE_URL is missing or
    the connection fails — every entrypoint script calls this first, so
    failing fast here means every script self-diagnoses the same way.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL is not set. See .env.example.", file=sys.stderr)
        sys.exit(1)

    try:
        return psycopg2.connect(
            database_url,
            # Detects a silently-dropped TCP connection (VPN blip, laptop
            # sleep, NAT/firewall idle-drop) instead of hanging forever
            # in "idle in transaction, waiting on ClientRead" — psycopg2
            # otherwise has no way to notice the network path died if
            # neither side sends a proper close. If no traffic for 15s,
            # the OS probes; after 3 failed probes (5s apart, ~30s total)
            # the connection raises OperationalError instead of hanging.
            keepalives=1,
            keepalives_idle=15,
            keepalives_interval=5,
            keepalives_count=3,
        )
    except Exception as exc:
        print(f"ERROR: could not connect to Supabase: {exc}", file=sys.stderr)
        sys.exit(1)
