"""OpenTofu state of the deployments, kept in the API database.

OpenTofu reaches it through the HTTP state backend (``routers/internal_tfstate``);
the API reads it for the resource views. There is no second copy: before,
the state lived in a separate ``postgres-tfstate`` database and a snapshot
on every task row (``tasks.tf_state``). It is Fernet-encrypted at rest
because it holds every attribute of every resource, generated passwords too.

Access is per task: a queued task carries a random token in its sealed
payload and only the token's hash on the row. The token opens the state of
that task's deployment for as long as the task is RUNNING, so a job can
neither reach another deployment's state nor keep access after it ended.

Locking mirrors OpenTofu's HTTP backend: a lock records the lock ID, the
lock info JSON and the holding task. A lock only blocks *other* tasks that
are still running:

- a lock whose task is no longer RUNNING is stale and may be overridden, so
  a crashed worker cannot block a deployment's state for good;
- a lock held by the requesting task itself does not block it. When the API
  was unreachable at the end of an apply, OpenTofu could neither write the
  state nor release its lock; the worker then pushes the saved
  ``errored.tfstate`` under a new lock ID in the same task, and must not be
  locked out by its own leftover lock.
"""

from __future__ import annotations

import hmac
import json
import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Task, TaskStatus, TerraformState
from app.services.jobs import state_token_hash
from app.utils.crypto import decrypt, encrypt


class LockConflict(Exception):
    """Someone else holds the state lock. Carries their lock info (JSON)."""

    def __init__(self, lock_info: str | None) -> None:
        super().__init__("state is locked")
        self.lock_info = lock_info


def authorize(db: Session, deployment_id: uuid.UUID, task_id: str, token: str) -> Task | None:
    """Return the running task of ``deployment_id`` that ``token`` belongs to, or None.

    None for a malformed ``task_id``, a task of another deployment, a task
    that is not RUNNING, or a wrong token (constant-time comparison).
    """
    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        return None
    task = db.get(Task, task_uuid)
    if task is None or task.deploymentId != deployment_id or task.status != TaskStatus.RUNNING:
        return None
    if not task.state_token_hash or not hmac.compare_digest(
        task.state_token_hash, state_token_hash(token)
    ):
        return None
    return task


def read(db: Session, deployment_id: uuid.UUID) -> str | None:
    """Return the decrypted state as JSON text, or None when there is none yet."""
    row = db.get(TerraformState, deployment_id)
    if row is None or row.state is None:
        return None
    return decrypt(row.state)


def _row_for_update(db: Session, deployment_id: uuid.UUID) -> TerraformState:
    """Return the deployment's state row, creating it if needed, locked ``FOR UPDATE``."""
    # Insert-if-missing first, so the row lock below always has a row to
    # take and two first writers cannot both create it.
    db.execute(insert(TerraformState).values(deploymentId=deployment_id).on_conflict_do_nothing())
    return db.scalars(
        select(TerraformState)
        .where(TerraformState.deploymentId == deployment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def _lock_is_stale(db: Session, row: TerraformState) -> bool:
    """Whether the row's lock may be overridden: no holder task, or it is no longer RUNNING."""
    if row.lock_task_id is None:
        return True
    holder = db.get(Task, row.lock_task_id)
    return holder is None or holder.status != TaskStatus.RUNNING


def _blocked(db: Session, row: TerraformState, lock_id: str | None, task: Task) -> bool:
    """Whether the row's lock keeps ``task`` (using ``lock_id``) out.

    Not when there is no lock, it is the same lock, it belongs to ``task``
    itself, or its task is no longer running.
    """
    if not row.lock_id or row.lock_id == lock_id:
        return False
    if row.lock_task_id == task.taskId:
        return False
    return not _lock_is_stale(db, row)


def write(db: Session, deployment_id: uuid.UUID, task: Task, state_json: str, lock_id: str | None) -> None:
    """Encrypt and store a new state, then commit.

    With a live lock of another task held, only a request carrying its
    ``lock_id`` may write; anyone else gets ``LockConflict``.
    """
    row = _row_for_update(db, deployment_id)
    if _blocked(db, row, lock_id, task):
        info = row.lock_info
        # Release the row lock now, as lock/unlock do, not at session end.
        db.rollback()
        raise LockConflict(info)
    row.state = encrypt(state_json)
    db.commit()


def lock(db: Session, deployment_id: uuid.UUID, task: Task, lock_info: str) -> None:
    """Take the state lock for ``task`` and commit.

    ``lock_info`` is OpenTofu's lock JSON; its ``ID`` becomes the lock id.
    Re-locking with the same ID succeeds. Raises LockConflict when another
    live lock is held, ValueError if ``lock_info`` has no ``ID``.
    """
    try:
        lock_id = str(json.loads(lock_info)["ID"])
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError("lock info without ID") from e
    row = _row_for_update(db, deployment_id)
    if _blocked(db, row, lock_id, task):
        info = row.lock_info
        db.rollback()
        raise LockConflict(info)
    row.lock_id = lock_id
    row.lock_info = lock_info
    row.lock_task_id = task.taskId
    db.commit()


def unlock(db: Session, deployment_id: uuid.UUID, task: Task, lock_info: str | None) -> None:
    """Release the lock and commit.

    An unlock with a different lock ID is a conflict (unless that lock is
    stale or the requesting task's own); an unlock without a lock ID
    (force-unlock) always succeeds.
    """
    row = _row_for_update(db, deployment_id)
    lock_id = None
    if lock_info:
        try:
            lock_id = str(json.loads(lock_info).get("ID"))
        except (ValueError, AttributeError):
            lock_id = None
    if lock_id and _blocked(db, row, lock_id, task):
        info = row.lock_info
        db.rollback()
        raise LockConflict(info)
    row.lock_id = None
    row.lock_info = None
    row.lock_task_id = None
    db.commit()


def drop(db: Session, deployment_id: uuid.UUID) -> None:
    """Forget the state. Does not commit; the caller does."""
    db.execute(delete(TerraformState).where(TerraformState.deploymentId == deployment_id))


class RestoreRefused(Exception):
    """Restoring a rescued state would lose data or race a running job."""


def restore(db: Session, deployment_id: uuid.UUID, rescued_json: str, *, force: bool = False) -> str:
    """Put a state the worker rescued (``errored.tfstate``) back; commits.

    For the case where the API was unreachable for longer than the worker
    kept retrying: the worker then keeps an encrypted copy of the state and
    logs its path (worker ``opentofu_executor._preserve``). Refuses — unless
    ``force`` — when the stored state belongs to another lineage or is the
    same or newer (higher serial). Always refuses while another task holds
    a live lock. Returns a one-line summary.
    """
    try:
        rescued = json.loads(rescued_json)
        serial, lineage = int(rescued["serial"]), str(rescued["lineage"])
    except (ValueError, KeyError, TypeError) as e:
        raise RestoreRefused(f"not an OpenTofu state: {e}") from e

    row = _row_for_update(db, deployment_id)
    if row.lock_id and not _lock_is_stale(db, row):
        db.rollback()
        raise RestoreRefused("a running task holds the state lock; wait until it has finished")
    if row.state is not None and not force:
        current = json.loads(decrypt(row.state))
        if current.get("lineage") != lineage:
            db.rollback()
            raise RestoreRefused("stored state has another lineage (use --force to overwrite)")
        if int(current.get("serial", -1)) >= serial:
            db.rollback()
            raise RestoreRefused(
                f"stored state is the same or newer (serial {current.get('serial')} >= {serial}); "
                "use --force to overwrite"
            )
    row.state = encrypt(rescued_json)
    row.lock_id = row.lock_info = row.lock_task_id = None
    db.commit()
    return f"restored state serial {serial} for deployment {deployment_id}"
