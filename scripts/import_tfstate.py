#!/usr/bin/env python3
"""One-off: copy OpenTofu states from the old ``postgres-tfstate`` database.

Before the Postgres queue, the worker kept each deployment's state in the
``pg`` backend of a separate database, one schema per deployment
(``deployment_<uuid with underscores>``, table ``states``, workspace
``default``). Now the API keeps it in ``terraform_states`` (encrypted) and
serves it to OpenTofu over HTTP.

Migration ``e7f8a9b0c1d2`` already seeds ``terraform_states`` from the last
``tasks.tf_state`` snapshot. This script overwrites those seeds with the
canonical state from the old database. Run it once during the cut-over,
after ``alembic upgrade head`` and before starting the new worker:

    TFSTATE_DATABASE_URL=postgresql://terraform:...@postgres-tfstate:5432/tfstate \\
        python scripts/import_tfstate.py [--dry-run]

Reading the state rows directly is what ``tofu state pull`` does for the pg
backend, without needing OpenTofu, the app repositories or credentials.
States of deployments unknown to the API database are reported and skipped.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

import psycopg
from psycopg import sql

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402
from app.database import libpq_url  # noqa: E402
from app.utils.crypto import encrypt  # noqa: E402

SCHEMA_PREFIX = "deployment_"


def deployment_id_for(schema: str) -> uuid.UUID | None:
    """The deployment UUID a ``deployment_<uuid_with_underscores>`` schema belongs to."""
    if not schema.startswith(SCHEMA_PREFIX):
        return None
    try:
        return uuid.UUID(schema[len(SCHEMA_PREFIX):].replace("_", "-"))
    except ValueError:
        return None


def read_old_states(conn: psycopg.Connection) -> dict[uuid.UUID, str]:
    """Every deployment's ``default`` workspace state from the old pg backend."""
    schemas = [
        row[0]
        for row in conn.execute(
            "SELECT table_schema FROM information_schema.tables "
            "WHERE table_name = 'states' AND table_schema LIKE 'deployment\\_%'"
        )
    ]
    states: dict[uuid.UUID, str] = {}
    for schema in schemas:
        deployment_id = deployment_id_for(schema)
        if deployment_id is None:
            continue
        row = conn.execute(
            sql.SQL("SELECT data FROM {}.states WHERE name = 'default'").format(sql.Identifier(schema))
        ).fetchone()
        if row and row[0]:
            states[deployment_id] = row[0]
    return states


def import_states(states: dict[uuid.UUID, str], target: psycopg.Connection, *, dry_run: bool) -> tuple[int, int]:
    """Upsert ``states`` into ``terraform_states``. Returns (imported, skipped)."""
    imported = skipped = 0
    for deployment_id, state in sorted(states.items(), key=lambda kv: str(kv[0])):
        known = target.execute(
            'SELECT 1 FROM deployments WHERE "deploymentId" = %s', (deployment_id,)
        ).fetchone()
        if not known:
            print(f"skip {deployment_id}: no such deployment in the API database")
            skipped += 1
            continue
        print(f"{'would import' if dry_run else 'import'} {deployment_id} ({len(state)} bytes)")
        if not dry_run:
            target.execute(
                """
                INSERT INTO terraform_states ("deploymentId", state, updated_at)
                VALUES (%s, %s, now() AT TIME ZONE 'utc')
                ON CONFLICT ("deploymentId")
                DO UPDATE SET state = EXCLUDED.state, updated_at = EXCLUDED.updated_at
                """,
                (deployment_id, encrypt(state)),
            )
        imported += 1
    return imported, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="only list what would be imported")
    args = parser.parse_args()

    source_url = os.environ.get("TFSTATE_DATABASE_URL")
    if not source_url:
        print("TFSTATE_DATABASE_URL (the old postgres-tfstate database) is not set.", file=sys.stderr)
        return 1

    with psycopg.connect(libpq_url(source_url)) as source:
        states = read_old_states(source)
    print(f"found {len(states)} state(s) in the old tfstate database")

    with psycopg.connect(libpq_url(settings.DATABASE_URL)) as target:
        imported, skipped = import_states(states, target, dry_run=args.dry_run)
        if not args.dry_run:
            target.commit()
    print(f"done: {imported} imported, {skipped} skipped{' (dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
