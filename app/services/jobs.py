"""The job contract between this API, which queues jobs, and the worker, which runs them.

A job is a row in ``tasks``: the API writes it PENDING with an encrypted
:class:`JobPayload`; the worker claims it, appends ``task_events`` rows while
it runs and records the result on the row. The worker repository mirrors
these names (``worker/app/job_tables.py``); the database grants in migration
``f8a9b0c1d2e3`` pin which columns it may touch.

Event types keep the names the live stream has always used, so the browser
side does not change.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, NotRequired, TypedDict

from app.models import Task, TaskStatus
from app.utils.crypto import encrypt

# Emitted while a job runs.
EVENT_PROGRESS = "task-progress"
EVENT_LOG = "task-log"
# Exactly one of these ends every task's event stream.
EVENT_SUCCEEDED = "task-succeeded"
EVENT_FAILED = "task-failed"
EVENT_CANCELLED = "task-revoked"
TERMINAL_EVENTS = frozenset({EVENT_SUCCEEDED, EVENT_FAILED, EVENT_CANCELLED})

# Why a task failed, for the UI: the job itself failed (OpenTofu, Packer,
# a bug in the worker), or the worker running it disappeared.
FAILURE_KIND_JOB = "worker_failure"
FAILURE_KIND_WORKER_LOST = "worker_lost"

FINISHED_STATUSES = (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED)

_TERMINAL = {
    TaskStatus.SUCCESS: (EVENT_SUCCEEDED, "success"),
    TaskStatus.FAILED: (EVENT_FAILED, "failed"),
    TaskStatus.CANCELLED: (EVENT_CANCELLED, "cancelled"),
}


class JobPayload(TypedDict):
    """Everything the worker needs to run one task. Stored encrypted on the row."""

    app_id: str
    app_git_link: str
    release: str | None
    user_vars: dict[str, Any]
    # team name -> [{"email": ...}]
    teams: dict[str, list[dict[str, str]]]
    # The caller's OpenStack credential, itself encrypted field by field.
    openstack_envelope: dict[str, Any]
    # Opens this deployment's state in /internal/tfstate while the task
    # runs; the row keeps only its hash. Added by ``task_service``.
    state_token: NotRequired[str]
    # Only for REDEPLOY: the resource address to replace.
    resource_address: NotRequired[str]


def state_token_hash(token: str) -> str:
    """What the task row stores of the state token."""
    return hashlib.sha256(token.encode()).hexdigest()


def seal_payload(payload: JobPayload) -> bytes:
    """Encrypt a job payload for ``Task.payload``."""
    return encrypt(json.dumps(payload))


def terminal_event(task: Task, failure_kind: str | None = None) -> tuple[str, dict[str, Any]]:
    """The last event of ``task``'s stream; ``task`` must be in a finished status.

    Returns ``(event_type, payload)``, in the shape the live stream has
    always sent when a task ended.
    """
    event_type, status = _TERMINAL[task.status]
    payload: dict[str, Any] = {
        "type": event_type,
        "deployment_id": str(task.deploymentId),
        "task_id": str(task.taskId),
        "task_type": task.type.value if task.type else None,
        "status": status,
    }
    if task.status == TaskStatus.FAILED and failure_kind:
        payload["failure_kind"] = failure_kind
    return event_type, payload
