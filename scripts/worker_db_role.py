#!/usr/bin/env python3
"""Let the worker log in: give ``appstore_worker`` the password from the environment.

Migration ``f8a9b0c1d2e3`` creates the ``appstore_worker`` role with its
column grants but without a password, because secrets do not belong in
migrations. Run this right after ``alembic upgrade head`` (the Makefile's
``migrate-dev`` and the staging playbook do); it is idempotent and also how
the password is rotated:

    WORKER_DB_PASSWORD=... python scripts/worker_db_role.py

Connects with ``DATABASE_URL`` (the migration user, which may alter roles).
"""

from __future__ import annotations

import os
import sys

import psycopg
from psycopg import sql

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402
from app.database import libpq_url  # noqa: E402

ROLE = "appstore_worker"


def main() -> int:
    password = os.environ.get("WORKER_DB_PASSWORD", "")
    if not password:
        print("WORKER_DB_PASSWORD is not set; nothing to do.", file=sys.stderr)
        return 1
    with psycopg.connect(libpq_url(settings.DATABASE_URL), autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,)).fetchone()
        if not exists:
            print(f"Role {ROLE} does not exist; run 'alembic upgrade head' first.", file=sys.stderr)
            return 1
        # ALTER ROLE takes no bind parameters; sql.Literal quotes the value.
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(ROLE), sql.Literal(password)
            )
        )
    print(f"Role {ROLE} can log in.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
