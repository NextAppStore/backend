"""worker role: the worker's database access, column by column

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-10-21 14:10:00.000000

The worker claims tasks, writes their events and records their results
directly in this database. It does that as ``appstore_worker``, which may
touch exactly the columns below and nothing else: no users, no credentials,
no OpenTofu state (that goes through the API's HTTP state backend with a
per-task token).

This list is the contract between backend and worker (two repositories,
no shared code): the worker's ``app/job_tables.py`` maps exactly these
columns, and ``tests/test_worker_contract.py`` fails when one of them is
renamed or dropped without updating this grant.

The role is created without a password (NOLOGIN). ``scripts/worker_db_role.py``
turns it into a login role with ``WORKER_DB_PASSWORD``; run it after
``alembic upgrade head``. Roles are cluster-wide, so the downgrade only
revokes this database's grants and leaves the role in place.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLE = "appstore_worker"

# tasks: read to claim (oldest PENDING first) and to check the claim is
# still ours; deploymentId/type pick the job. "taskId" and "deploymentId"
# are also read by the task_events trigger, which runs as the worker.
TASKS_SELECT = ('"taskId"', '"deploymentId"', "type", "status", "payload", "claimed_by", "created_at")
# tasks: claim (status, claimed_by, lease_until, started_at, clear payload),
# progress (current_phase, progress_pct) and result (status, finished_at,
# logs, outputs).
TASKS_UPDATE = (
    "status",
    "payload",
    "claimed_by",
    "lease_until",
    "started_at",
    "finished_at",
    "logs",
    "outputs",
    "current_phase",
    "progress_pct",
)
TASK_EVENTS_INSERT = ('"taskId"', "type", "payload", "created_at")


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} NOLOGIN;
            END IF;
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO {ROLE}', current_database());
        END $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    op.execute(f"GRANT SELECT ({', '.join(TASKS_SELECT)}) ON tasks TO {ROLE}")
    op.execute(f"GRANT UPDATE ({', '.join(TASKS_UPDATE)}) ON tasks TO {ROLE}")
    op.execute(f"GRANT INSERT ({', '.join(TASK_EVENTS_INSERT)}) ON task_events TO {ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE task_events_id_seq TO {ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON SEQUENCE task_events_id_seq FROM {ROLE}")
    op.execute(f"REVOKE ALL ON task_events FROM {ROLE}")
    op.execute(f"REVOKE ALL ON tasks FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE}")
    op.execute(
        f"""
        DO $$
        BEGIN
            EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {ROLE}', current_database());
        END $$;
        """
    )
