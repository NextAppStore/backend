"""postgres job queue: task payload/lease/finalization, task_events, tfstate, lti login states

Revision ID: e7f8a9b0c1d2
Revises: 4ce9d3e29956
Create Date: 2026-10-21 14:00:00.000000

Postgres replaces RabbitMQ, Redis, Celery and the separate tfstate
database (see "Konzept Backend & Worker vereinfachen"):

* ``tasks`` becomes the job queue: ``payload`` (sealed job), ``claimed_by``
  and ``lease_until`` (worker lease), ``finalized_at`` (follow-up work done
  once), ``state_token_hash`` (access to the OpenTofu state while running).
  ``celeryTaskId`` goes away, and so does ``tf_state``: the state lives in
  ``terraform_states`` only.
* ``task_events`` carries progress/log/terminal events for the live stream.
* Triggers send ``task_queued``, ``task_events`` and ``task_finished``
  notifications (see ``app.models``, QUEUE NOTIFICATIONS).
* ``terraform_states`` backs the HTTP state backend under
  ``/internal/tfstate``. Seeded here from the newest ``tasks.tf_state``
  snapshot of each deployment so no deployment is left without a state;
  ``scripts/import_tfstate.py`` then copies the canonical state from the old
  ``postgres-tfstate`` database over it.
* ``lti_login_states`` replaces the Redis OIDC nonce store.
* ``uq_tasks_active_per_deployment`` is created again: migration
  73fd123a60aa dropped it by autogenerate.

Cut-over: tasks still PENDING or RUNNING belong to the old Celery pipeline,
which is gone after this migration; nothing would ever finish them. They
are failed here (and marked finalized, so no mails go out for them).
Finished tasks are marked finalized too, otherwise the finalizer would send
the access mails of every past deploy again.

Downgrade restores the columns but not their data (celery ids, state
snapshots) and drops the new tables.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "4ce9d3e29956"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CUTOVER_MESSAGE = (
    "Abgebrochen bei der Umstellung auf die Postgres-Queue: der Auftrag lief "
    "noch in der alten Celery-Pipeline. Bitte prüfen Sie den Zustand der "
    "Ressourcen und starten Sie die Aktion bei Bedarf erneut."
)

# Same SQL as app.models (QUEUE NOTIFICATIONS); copied, not imported, so this
# migration keeps doing what it did when the model changes later.
TRIGGER_FUNCTIONS = """
CREATE OR REPLACE FUNCTION appstore_notify_task_queued() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('task_queued', '');
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION appstore_notify_task_finished() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('task_finished', NEW."taskId"::text);
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION appstore_notify_task_event() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify(
        'task_events',
        (SELECT "deploymentId"::text FROM tasks WHERE "taskId" = NEW."taskId")
    );
    RETURN NULL;
END $$;
"""

TRIGGERS = """
CREATE TRIGGER tasks_notify_queued
    AFTER INSERT ON tasks
    FOR EACH ROW WHEN (NEW.status = 'PENDING')
    EXECUTE FUNCTION appstore_notify_task_queued();

CREATE TRIGGER tasks_notify_finished
    AFTER UPDATE OF status ON tasks
    FOR EACH ROW WHEN (
        OLD.status IS DISTINCT FROM NEW.status
        AND NEW.status IN ('SUCCESS', 'FAILED', 'CANCELLED')
    )
    EXECUTE FUNCTION appstore_notify_task_finished();

CREATE TRIGGER task_events_notify
    AFTER INSERT ON task_events
    FOR EACH ROW
    EXECUTE FUNCTION appstore_notify_task_event();
"""


def _seed_terraform_states() -> None:
    """Copy each deployment's newest ``tasks.tf_state`` snapshot, encrypted."""
    # Imported here: only this step needs the Fernet key.
    from app.utils.crypto import encrypt

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT DISTINCT ON ("deploymentId") "deploymentId", tf_state
            FROM tasks
            WHERE tf_state IS NOT NULL AND tf_state <> ''
            ORDER BY "deploymentId", created_at DESC
            """
        )
    ).all()
    for deployment_id, state in rows:
        bind.execute(
            sa.text(
                'INSERT INTO terraform_states ("deploymentId", state, updated_at) '
                "VALUES (:d, :s, now() AT TIME ZONE 'utc') ON CONFLICT DO NOTHING"
            ),
            {"d": deployment_id, "s": encrypt(state)},
        )


def upgrade() -> None:
    # --- tasks: queue columns --------------------------------------------
    op.add_column("tasks", sa.Column("payload", sa.LargeBinary(), nullable=True))
    op.add_column("tasks", sa.Column("state_token_hash", sa.String(length=64), nullable=True))
    op.add_column("tasks", sa.Column("claimed_by", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("lease_until", sa.DateTime(), nullable=True))
    op.add_column("tasks", sa.Column("finalized_at", sa.DateTime(), nullable=True))

    # --- cut-over: old pipeline's tasks ------------------------------------
    op.execute(
        sa.text(
            """
            UPDATE tasks
            SET status = 'FAILED',
                finished_at = now() AT TIME ZONE 'utc',
                logs = CASE WHEN logs IS NULL OR logs = '' THEN :msg
                            ELSE logs || E'\\n\\n' || :msg END
            WHERE status IN ('PENDING', 'RUNNING')
            """
        ).bindparams(msg=CUTOVER_MESSAGE)
    )
    op.execute(
        "UPDATE tasks SET finalized_at = COALESCE(finished_at, now() AT TIME ZONE 'utc')"
    )

    # --- tasks: indexes ----------------------------------------------------
    op.execute("DROP INDEX IF EXISTS uq_tasks_active_per_deployment")
    op.create_index(
        "uq_tasks_active_per_deployment",
        "tasks",
        ["deploymentId"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )
    op.create_index(
        "ix_tasks_pending", "tasks", ["created_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "ix_tasks_unfinalized", "tasks", ["finished_at"],
        postgresql_where=sa.text("finalized_at IS NULL"),
    )
    op.create_index(
        "ix_tasks_running_lease", "tasks", ["lease_until"],
        postgresql_where=sa.text("status = 'RUNNING'"),
    )

    # --- task_events -------------------------------------------------------
    op.create_table(
        "task_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("taskId", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["taskId"], ["tasks.taskId"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_task_events_taskId"), "task_events", ["taskId"], unique=False)

    # --- terraform_states --------------------------------------------------
    op.create_table(
        "terraform_states",
        sa.Column("deploymentId", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.LargeBinary(), nullable=True),
        sa.Column("lock_id", sa.String(), nullable=True),
        sa.Column("lock_info", sa.Text(), nullable=True),
        sa.Column("lock_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["deploymentId"], ["deployments.deploymentId"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("deploymentId"),
    )
    _seed_terraform_states()

    # --- lti_login_states --------------------------------------------------
    op.create_table(
        "lti_login_states",
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("nonce", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("state"),
    )
    op.create_index(
        op.f("ix_lti_login_states_expires_at"), "lti_login_states", ["expires_at"], unique=False
    )

    # --- notifications -----------------------------------------------------
    op.execute(TRIGGER_FUNCTIONS)
    op.execute(TRIGGERS)

    # --- drop what the old pipeline needed ---------------------------------
    op.drop_column("tasks", "celeryTaskId")
    op.drop_column("tasks", "tf_state")


def downgrade() -> None:
    op.add_column("tasks", sa.Column("tf_state", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("celeryTaskId", sa.String(), nullable=True))

    op.execute("DROP TRIGGER IF EXISTS task_events_notify ON task_events")
    op.execute("DROP TRIGGER IF EXISTS tasks_notify_finished ON tasks")
    op.execute("DROP TRIGGER IF EXISTS tasks_notify_queued ON tasks")
    op.execute("DROP FUNCTION IF EXISTS appstore_notify_task_event()")
    op.execute("DROP FUNCTION IF EXISTS appstore_notify_task_finished()")
    op.execute("DROP FUNCTION IF EXISTS appstore_notify_task_queued()")

    op.drop_index(op.f("ix_lti_login_states_expires_at"), table_name="lti_login_states")
    op.drop_table("lti_login_states")
    op.drop_table("terraform_states")
    op.drop_index(op.f("ix_task_events_taskId"), table_name="task_events")
    op.drop_table("task_events")

    op.drop_index("ix_tasks_running_lease", table_name="tasks")
    op.drop_index("ix_tasks_unfinalized", table_name="tasks")
    op.drop_index("ix_tasks_pending", table_name="tasks")
    # uq_tasks_active_per_deployment stays: the schema had it before
    # 73fd123a60aa dropped it by accident.

    op.drop_column("tasks", "finalized_at")
    op.drop_column("tasks", "lease_until")
    op.drop_column("tasks", "claimed_by")
    op.drop_column("tasks", "state_token_hash")
    op.drop_column("tasks", "payload")
