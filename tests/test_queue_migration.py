"""Migration e7f8a9b0c1d2 against data as the Celery pipeline left it.

Builds a database at the revision before the Postgres queue, fills it the
way production looks at cut-over time, upgrades, and checks the cut-over
rules from the migration's docstring:

* tasks still PENDING/RUNNING in the old pipeline are failed (nobody would
  ever finish them) and nobody gets mails for them;
* every existing task is finalized, so the finalizer does not resend the
  access mails of past deploys;
* each deployment's newest ``tasks.tf_state`` snapshot is carried over into
  ``terraform_states``, encrypted;
* two active tasks of one deployment (possible since 73fd123a60aa dropped
  the unique index) do not break the index creation;
* downgrade runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from app.database import libpq_url, sqlalchemy_url
from app.utils.crypto import decrypt
from tests.conftest import _TEST_DB_URL

BACKEND_ROOT = Path(__file__).resolve().parents[1]
BEFORE_QUEUE = "4ce9d3e29956"


def _alembic(url: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def old_db_url():
    admin_url = libpq_url(_TEST_DB_URL)
    name = f"migr_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = make_url(sqlalchemy_url(_TEST_DB_URL)).set(database=name).render_as_string(hide_password=False)
    try:
        _alembic(url, "upgrade", BEFORE_QUEUE)
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.mark.integration
def test_cutover_fails_old_inflight_tasks_finalizes_history_and_moves_state(old_db_url):
    user_id, app_id, dep_a, dep_b = (uuid.uuid4() for _ in range(4))
    t_old, t_new, t_running, t_pending = (uuid.uuid4() for _ in range(4))
    with psycopg.connect(libpq_url(old_db_url)) as conn:
        conn.execute(
            """INSERT INTO users ("userId", email, username, role) VALUES (%s, 'm@x.de', 'm', 'STUDENT')""",
            (user_id,),
        )
        conn.execute(
            """INSERT INTO apps ("appId", name, "userId", is_private) VALUES (%s, 'a', %s, false)""",
            (app_id, user_id),
        )
        for dep in (dep_a, dep_b):
            conn.execute(
                """INSERT INTO deployments ("deploymentId", name, "userId", "appId") VALUES (%s, 'd', %s, %s)""",
                (dep, user_id, app_id),
            )
        rows = [
            # dep_a: two finished tasks with state snapshots, the newer wins
            (t_old, dep_a, "DEPLOY", "SUCCESS", "c-1", '{"serial": 1}', "2026-01-01 10:00", "2026-01-01 10:05"),
            (t_new, dep_a, "PAUSE", "SUCCESS", "c-2", '{"serial": 2}', "2026-01-02 10:00", "2026-01-02 10:05"),
            # dep_b: two "active" tasks at once (index was missing) from Celery
            (t_running, dep_b, "DEPLOY", "RUNNING", "c-3", None, "2026-01-03 10:00", None),
            (t_pending, dep_b, "DESTROY", "PENDING", None, None, "2026-01-03 11:00", None),
        ]
        for task_id, dep, typ, status, celery_id, state, created, finished in rows:
            conn.execute(
                """INSERT INTO tasks ("taskId", "deploymentId", type, status, "celeryTaskId",
                                      tf_state, created_at, finished_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (task_id, dep, typ, status, celery_id, state, created, finished),
            )
        conn.commit()

    _alembic(old_db_url, "upgrade", "head")

    with psycopg.connect(libpq_url(old_db_url)) as conn:
        statuses = dict(conn.execute('SELECT "taskId", status::text FROM tasks').fetchall())
        assert statuses[t_running] == "FAILED"
        assert statuses[t_pending] == "FAILED"
        assert statuses[t_new] == "SUCCESS"
        logs = conn.execute('SELECT logs FROM tasks WHERE "taskId" = %s', (t_running,)).fetchone()[0]
        assert "Umstellung auf die Postgres-Queue" in logs
        assert conn.execute("SELECT count(*) FROM tasks WHERE finalized_at IS NULL").fetchone()[0] == 0

        states = dict(conn.execute('SELECT "deploymentId", state FROM terraform_states').fetchall())
        assert set(states) == {dep_a}
        assert decrypt(bytes(states[dep_a])) == '{"serial": 2}'

        columns = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tasks'"
        )}
        assert "celeryTaskId" not in columns and "tf_state" not in columns
        assert {"payload", "claimed_by", "lease_until", "finalized_at", "state_token_hash"} <= columns

        # The restored index holds again.
        with pytest.raises(psycopg.errors.UniqueViolation):
            for _ in range(2):
                conn.execute(
                    """INSERT INTO tasks ("taskId", "deploymentId", type, status, created_at)
                       VALUES (%s, %s, 'DEPLOY', 'PENDING', now())""",
                    (uuid.uuid4(), dep_b),
                )
        conn.rollback()

    _alembic(old_db_url, "downgrade", BEFORE_QUEUE)
    with psycopg.connect(libpq_url(old_db_url)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )}
        assert not {"task_events", "terraform_states", "lti_login_states"} & tables
