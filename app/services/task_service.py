"""Queue a task for the worker.

The ``tasks`` table is the job queue. ``prepare_task_in_tx`` inserts a
PENDING row carrying the sealed job payload in the caller's transaction;
committing it is all it takes to hand the job to a worker (a trigger sends
``task_queued`` on commit). Deployment, teams and the task therefore become
visible together or not at all, and there is no second system that could
have accepted a job the database never saw, or the other way round.
"""
from __future__ import annotations

import logging
import secrets
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crud import tasks as crud_tasks
from app.i18n import get_current_locale
from app.models import Task, TaskStatus, TaskType
from app.services.jobs import JobPayload, seal_payload, state_token_hash

logger = logging.getLogger(__name__)


# Name of the Postgres partial unique index backing the "one active
# task per deployment" rule. String-matched when translating
# IntegrityError → ActiveTaskExistsError so other unique-constraint
# violations aren't swallowed.
_ACTIVE_TASK_UNIQUE_INDEX = "uq_tasks_active_per_deployment"


class ActiveTaskExistsError(Exception):
    """A PENDING/RUNNING task already exists for this deployment."""


def prepare_task_in_tx(
    db: Session,
    deployment_id: uuid.UUID,
    task_type: TaskType,
    payload: JobPayload,
) -> Task:
    """Insert a PENDING task row with its sealed job payload in the caller's transaction.

    A fresh random ``state_token`` is added to ``payload``; only its hash
    is stored on the row. The worker uses the token to reach this
    deployment's OpenTofu state while the task runs (see
    ``tf_state_store.authorize``).

    Does NOT call `db.commit()` — the caller commits the surrounding state
    alongside this row, so that deployment + teams + task are all visible
    (or all rolled back) atomically.

    Raises `ActiveTaskExistsError` if the deployment already has a
    PENDING/RUNNING task. A Postgres partial unique index enforces this
    at the DB level too: the pre-check catches the common case, and the
    ``except IntegrityError`` around ``db.flush()`` catches the racy one
    (two concurrent requests both pass the pre-check).
    """
    existing = crud_tasks.get_tasks(db, deployment_id=deployment_id)
    for task in existing:
        if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            raise ActiveTaskExistsError(
                f"Deployment {deployment_id} already has active task {task.taskId}"
            )

    state_token = secrets.token_urlsafe(32)
    db_task = Task(
        deploymentId=deployment_id,
        type=task_type,
        status=TaskStatus.PENDING,
        payload=seal_payload({**payload, "state_token": state_token}),
        state_token_hash=state_token_hash(state_token),
        # Language of the triggering request; later texts that have no
        # request context (finalizer, mails) use it.
        locale=get_current_locale(),
    )
    db.add(db_task)
    try:
        db.flush()
    except IntegrityError as e:
        # Race: another transaction inserted a PENDING/RUNNING task for
        # the same deployment between our pre-check and flush. Translate
        # the constraint violation into the domain exception so the
        # caller's 409 branch fires.
        if _ACTIVE_TASK_UNIQUE_INDEX in str(e.orig):
            raise ActiveTaskExistsError(
                f"Deployment {deployment_id} already has an active task "
                "(detected via DB unique constraint)"
            ) from e
        raise
    db.refresh(db_task)
    logger.info("Task %s (%s) queued for deployment %s", db_task.taskId, task_type.value, deployment_id)
    return db_task
