"""Follow-up work for finished tasks, and failing tasks whose worker died.

**Finalize.** A finished task gets its follow-up work exactly once — the
access mails after a deploy, the soft-delete and dropping the OpenTofu
state after a destroy — and ``finalized_at`` is set. Every API process
hears ``task_finished`` (see ``db_events``), but the row is claimed with
``FOR UPDATE SKIP LOCKED … WHERE finalized_at IS NULL``, so only one of
them does the work. Before, every uvicorn process ran its own Celery event
listener and every one of them sent the mails.

**Reap.** A RUNNING task whose lease ran out belongs to a worker that
stopped renewing it — crashed, killed or cut off from the database. The
task is failed with a terminal event so open streams close. Nothing is
retried: a half-applied OpenTofu run needs a person to look at it.

``run_reaper`` is started from the FastAPI lifespan in ``main.py``. Each
tick also finalizes whatever is still unfinalized, as a safety net for a
process that died between claiming a task and committing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.crud import deployments as crud_deployments
from app.database import SessionLocal
from app.models import Task, TaskEvent, TaskStatus, TaskType
from app.services import deployment_notifier, tf_state_store
from app.services.jobs import FAILURE_KIND_WORKER_LOST, FINISHED_STATUSES, terminal_event
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

# Rows per pass. A backlog is worked off over consecutive passes.
BATCH = 20

WORKER_LOST_MESSAGE = (
    "Der Worker, der diesen Auftrag ausgeführt hat, antwortet nicht mehr "
    "(abgestürzt oder neu gestartet). Der Auftrag wurde abgebrochen und wird "
    "nicht automatisch wiederholt. Bitte prüfen Sie den Zustand der "
    "Ressourcen und starten Sie die Aktion bei Bedarf erneut."
)


# ----------------------------------------------------------------
# Finalize
# ----------------------------------------------------------------
def _outputs(task: Task) -> dict[str, Any] | None:
    """The task's OpenTofu outputs as a dict, or None."""
    if not task.outputs:
        return None
    try:
        value = json.loads(task.outputs)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _drop_older_events(db: Session, task: Task) -> None:
    """Delete the events of the deployment's earlier tasks.

    The live stream only ever shows the latest task, and the full log of
    every task is kept in ``tasks.logs``. The finished task's own events
    stay, so a stream that is still reading them is not cut short.
    """
    earlier = select(Task.taskId).where(Task.deploymentId == task.deploymentId, Task.taskId != task.taskId)
    db.execute(delete(TaskEvent).where(TaskEvent.taskId.in_(earlier)))


def _claim(db: Session, task_id: uuid.UUID | None) -> Task | None:
    """Lock one finished, unfinalized task (a given one, or the oldest); None if there is none free."""
    query = (
        select(Task)
        .where(Task.status.in_(FINISHED_STATUSES), Task.finalized_at.is_(None))
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if task_id is not None:
        query = query.where(Task.taskId == task_id)
    else:
        query = query.order_by(Task.finished_at)
    return db.scalars(query).first()


def _finalize_claimed(db: Session, task: Task) -> None:
    """Mark ``task`` finalized and run its follow-up work; commits.

    For a successful deploy the mark is committed *before* the mails go
    out: a crash in between loses the mails (the owner can resend them
    from the UI) rather than sending them twice. Everything else is
    idempotent and commits together with the mark.
    """
    task.finalized_at = utcnow()
    _drop_older_events(db, task)
    succeeded = task.status == TaskStatus.SUCCESS

    if succeeded and task.type == TaskType.DESTROY:
        # The OpenStack resources are gone: hide the deployment but keep
        # the row and its tasks for the record. The state has nothing left
        # to describe and held the resources' secrets.
        tf_state_store.drop(db, task.deploymentId)
        crud_deployments.soft_delete_deployment(db, task.deploymentId)  # commits
        logger.info("deployment %s soft-deleted after successful destroy", task.deploymentId)
    db.commit()

    if succeeded and task.type == TaskType.DEPLOY:
        try:
            deployment_notifier.notify_deployment_succeeded(
                db, task.deploymentId, terraform_outputs=_outputs(task)
            )
        except Exception:
            # Left finalized: retrying would resend the mails that did go out.
            logger.exception("deploy notification for task %s failed", task.taskId)


def finalize_task(db: Session, task_id: uuid.UUID) -> bool:
    """Finalize one task if it is finished, unfinalized and not being finalized elsewhere.

    Returns whether this call did it.
    """
    task = _claim(db, task_id)
    if task is None:
        db.rollback()
        return False
    _finalize_claimed(db, task)
    return True


def finalize_pending(db: Session) -> int:
    """Finalize up to ``BATCH`` unfinalized tasks, oldest first. Returns how many."""
    done = 0
    for _ in range(BATCH):
        task = _claim(db, None)
        if task is None:
            db.rollback()
            break
        task_id = task.taskId
        try:
            _finalize_claimed(db, task)
        except Exception:
            # A broken follow-up must not block the queue behind it: mark it
            # finalized and move on (logged, the UI can redo what is missing).
            logger.exception("finalizing task %s failed", task_id)
            db.rollback()
            db.execute(update(Task).where(Task.taskId == task_id).values(finalized_at=utcnow()))
            db.commit()
        done += 1
    return done


# ----------------------------------------------------------------
# Reap
# ----------------------------------------------------------------
def reap_expired(db: Session) -> int:
    """Fail RUNNING tasks whose lease has run out. Returns how many.

    Handles at most ``BATCH`` tasks per call. Appends ``WORKER_LOST_MESSAGE``
    to the task log and writes the terminal event (so live streams end).
    The status change fires ``task_finished``, which finalizes the task.
    """
    tasks = db.scalars(
        select(Task)
        .where(Task.status == TaskStatus.RUNNING, Task.lease_until < utcnow())
        .with_for_update(skip_locked=True)
        .limit(BATCH)
    ).all()
    for task in tasks:
        logger.warning("task %s: lease of worker %s expired, failing it", task.taskId, task.claimed_by)
        task.status = TaskStatus.FAILED
        task.finished_at = utcnow()
        task.lease_until = None
        task.logs = f"{task.logs}\n\n{WORKER_LOST_MESSAGE}" if task.logs else WORKER_LOST_MESSAGE
        event_type, payload = terminal_event(task, FAILURE_KIND_WORKER_LOST)
        db.add(TaskEvent(taskId=task.taskId, type=event_type, payload=json.dumps(payload)))
    db.commit()
    return len(tasks)


# ----------------------------------------------------------------
# Entry points for the lifespan / db_events
# ----------------------------------------------------------------
def finalize_task_by_id(task_id: str) -> None:
    """``task_finished`` handler: finalize one task in its own session."""
    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        logger.warning("task_finished with malformed payload %r", task_id)
        return
    with SessionLocal() as db:
        finalize_task(db, task_uuid)


def catch_up() -> None:
    """Finalize everything still unfinalized; run at start and after every reconnect."""
    with SessionLocal() as db:
        while finalize_pending(db) == BATCH:
            pass


def reaper_tick() -> None:
    """One reaper pass: fail tasks of dead workers, then the finalize safety net."""
    with SessionLocal() as db:
        reap_expired(db)
    with SessionLocal() as db:
        finalize_pending(db)


async def run_reaper() -> None:
    """Run ``reaper_tick`` every ``TASK_REAPER_INTERVAL_SECONDS`` until cancelled."""
    interval = settings.TASK_REAPER_INTERVAL_SECONDS
    logger.info("task reaper running every %ss", interval)
    while True:
        try:
            # The database work is synchronous; keep it off the event loop.
            await asyncio.to_thread(reaper_tick)
        except Exception:
            logger.exception("task reaper pass failed")
        await asyncio.sleep(interval)
