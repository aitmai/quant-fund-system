"""
GUI-specific DB connection helper.

Deliberately separate from src/db.py's get_connection(): that one calls
sys.exit(1) on failure, which is correct for a one-shot cron script but
would kill the entire Flask process on a single bad request. Here, a
connection failure raises a normal exception that a route can catch and
render as a friendly error state instead.

Uses RealDictCursor so query results come back as dicts (row['ticker'])
rather than positional tuples — templates read far more clearly this way,
and it's one less thing to get wrong when a SELECT's column order changes.
"""

import os

import psycopg2
import psycopg2.extras


class DatabaseUnavailable(Exception):
    """Raised when DATABASE_URL is missing or the connection fails."""


def get_db_connection():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise DatabaseUnavailable("DATABASE_URL is not set. See .env.example.")
    try:
        return psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as exc:
        raise DatabaseUnavailable(f"Could not connect to Supabase: {exc}") from exc
