"""The contract with the worker repository, checked against the real migrations.

Backend and worker are separate repositories without shared code. The
worker writes into this database as ``appstore_worker``; what it may touch
is granted column by column in migration ``f8a9b0c1d2e3``, and the worker's
``app/job_tables.py`` maps exactly these columns.

These tests build a fresh database with ``alembic upgrade head`` (not
``create_all``) and check:

* the worker role has exactly the contracted privileges, no more, no less —
  renaming or dropping a contracted column, or widening the grant, fails
  here and has to be mirrored in the worker repository;
* the worker's statements (claim, lease, events, result) work under that
  role, and its triggers fire;
* the migrations produce the schema the models describe for the queue
  tables (so ``create_all`` in the other tests is a faithful stand-in).

If you change this contract, change ``worker/app/job_tables.py`` in the
same release, worker first (frontend/AGENTS.md, "Worker → Backend").
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from psycopg import errors, sql
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.database import Base, libpq_url, sqlalchemy_url
from tests.conftest import _TEST_DB_URL

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ROLE = "appstore_worker"

# --- The contract (mirror of worker/app/job_tables.py) ----------------------
WORKER_TASKS_SELECT = {"taskId", "deploymentId", "type", "status", "payload", "claimed_by", "created_at"}
WORKER_TASKS_UPDATE = {
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
}
WORKER_TASK_EVENTS_INSERT = {"taskId", "type", "payload", "created_at"}

QUEUE_TABLES = {"tasks", "task_events", "terraform_states", "lti_login_states"}


@pytest.fixture(scope="module")
def migrated_db_url():
    """A fresh database migrated with ``alembic upgrade head``; dropped afterwards."""
    admin_url = libpq_url(_TEST_DB_URL)
    name = f"contract_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = make_url(sqlalchemy_url(_TEST_DB_URL)).set(database=name).render_as_string(hide_password=False)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND_ROOT,
            env={**os.environ, "DATABASE_URL": url},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position",
            (table,),
        )
    ]


def _allowed(conn: psycopg.Connection, table: str, privilege: str) -> set[str]:
    return {
        col
        for col in _columns(conn, table)
        if conn.execute(
            "SELECT has_column_privilege(%s, %s, %s, %s)", (ROLE, table, col, privilege)
        ).fetchone()[0]
    }


@pytest.mark.integration
def test_worker_role_has_exactly_the_contracted_column_privileges(migrated_db_url):
    with psycopg.connect(libpq_url(migrated_db_url)) as conn:
        assert _allowed(conn, "tasks", "SELECT") == WORKER_TASKS_SELECT
        assert _allowed(conn, "tasks", "UPDATE") == WORKER_TASKS_UPDATE
        assert _allowed(conn, "tasks", "INSERT") == set()
        assert _allowed(conn, "task_events", "INSERT") == WORKER_TASK_EVENTS_INSERT
        assert _allowed(conn, "task_events", "SELECT") == set()
        assert _allowed(conn, "task_events", "UPDATE") == set()
        assert not conn.execute("SELECT has_table_privilege(%s, 'tasks', 'DELETE')", (ROLE,)).fetchone()[0]
        # Nothing outside the queue: no users, credentials or state.
        for table in ("users", "user_openstack_credentials", "deployments", "terraform_states", "apps"):
            assert not conn.execute(
                "SELECT has_any_column_privilege(%s, %s, 'SELECT,INSERT,UPDATE')", (ROLE, table)
            ).fetchone()[0], table


@pytest.mark.integration
def test_worker_statements_work_under_the_worker_role(migrated_db_url):
    """Run the worker's own statements as ``appstore_worker``: claim, lease, events, result."""
    with psycopg.connect(libpq_url(migrated_db_url)) as conn:
        # Seed as the owner: a user, app, deployment and a queued task.
        user_id, app_id, dep_id, task_id = (uuid.uuid4() for _ in range(4))
        conn.execute(
            """INSERT INTO users ("userId", email, username, role) VALUES (%s, 'w@x.de', 'w', 'STUDENT')""",
            (user_id,),
        )
        conn.execute(
            """INSERT INTO apps ("appId", name, "userId", is_private) VALUES (%s, 'a', %s, false)""",
            (app_id, user_id),
        )
        conn.execute(
            """INSERT INTO deployments ("deploymentId", name, "userId", "appId") VALUES (%s, 'd', %s, %s)""",
            (dep_id, user_id, app_id),
        )
        conn.execute(
            """INSERT INTO tasks ("taskId", "deploymentId", type, status, payload, created_at)
               VALUES (%s, %s, 'DEPLOY', 'PENDING', 'sealed', now())""",
            (task_id, dep_id),
        )
        conn.commit()

        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(ROLE)))
        conn.execute("LISTEN task_events")
        conn.execute("LISTEN task_finished")
        conn.commit()

        # claim (as in worker/app/job_queue.py)
        row = conn.execute(
            """SELECT "taskId", "deploymentId", type, status, payload, claimed_by FROM tasks
               WHERE status = 'PENDING' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"""
        ).fetchone()
        assert row is not None and row[0] == task_id
        conn.execute(
            """UPDATE tasks SET status = 'RUNNING', payload = NULL, claimed_by = 'w-0',
               started_at = now(), lease_until = now() + interval '2 minutes' WHERE "taskId" = %s""",
            (task_id,),
        )
        conn.commit()
        # lease renewal
        renewed = conn.execute(
            """UPDATE tasks SET lease_until = now() + interval '2 minutes'
               WHERE "taskId" = %s AND claimed_by = 'w-0' AND status = 'RUNNING'""",
            (task_id,),
        ).rowcount
        assert renewed == 1
        # events + progress columns
        conn.execute(
            """INSERT INTO task_events ("taskId", type, payload, created_at)
               VALUES (%s, 'task-log', '{}', now())""",
            (task_id,),
        )
        conn.execute(
            """UPDATE tasks SET current_phase = 'GIT_CLONE', progress_pct = 20 WHERE "taskId" = %s""",
            (task_id,),
        )
        conn.commit()
        # result
        conn.execute(
            """SELECT status, claimed_by FROM tasks WHERE "taskId" = %s FOR UPDATE""", (task_id,)
        ).fetchone()
        conn.execute(
            """UPDATE tasks SET status = 'SUCCESS', finished_at = now(), lease_until = NULL,
               logs = '[]', outputs = '{}' WHERE "taskId" = %s""",
            (task_id,),
        )
        conn.commit()

        channels = {(n.channel, n.payload) for n in conn.notifies(timeout=1, stop_after=2)}
        assert ("task_events", str(dep_id)) in channels
        assert ("task_finished", str(task_id)) in channels

        # ... and nothing beyond the contract.
        for statement in (
            'SELECT "userId" FROM users',
            'SELECT state FROM terraform_states',
            'SELECT logs FROM tasks',
            'UPDATE tasks SET finalized_at = now()',
            'UPDATE tasks SET state_token_hash = NULL',
            'SELECT id FROM task_events',
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()


@pytest.mark.integration
def test_migrations_match_models_for_queue_tables(migrated_db_url):
    """Autogenerate finds nothing to change for the queue tables after ``upgrade head``."""
    engine = create_engine(migrated_db_url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"compare_type": True})
            diffs = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    def table_of(diff) -> str | None:
        """Table name a compare_metadata diff entry refers to."""
        if isinstance(diff, list):  # modify_* entries come grouped per column
            diff = diff[0]
        kind, obj = diff[0], diff[1]
        if kind in ("add_table", "remove_table"):
            return obj.name
        if kind in ("add_column", "remove_column") or kind.startswith("modify_"):
            return diff[2]
        table = getattr(obj, "table", None)
        return getattr(table, "name", None)

    queue_diffs = [d for d in diffs if table_of(d) in QUEUE_TABLES]
    assert queue_diffs == []


@pytest.mark.integration
def test_queue_triggers_exist_after_migration(migrated_db_url):
    with psycopg.connect(libpq_url(migrated_db_url)) as conn:
        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"
            )
        }
    assert {"tasks_notify_queued", "tasks_notify_finished", "task_events_notify"} <= triggers
