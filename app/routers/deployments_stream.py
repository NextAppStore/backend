"""Server-Sent-Events stream for one deployment's live progress + logs.

Split out of :mod:`app.routers.deployments` because it shares nothing
with the CRUD handlers there: it is async where they are sync, it holds
a connection open for minutes, and its whole vocabulary (pubsub queues,
keepalive frames, SSE wire format) is its own.

Mounted as a sub-router on the deployments router, so the URL prefix and
the OpenAPI tag are unchanged — ``GET /deployments/{id}/stream``.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.crud import deployments as crud_deployments
from app.database import get_db
from app.models import TaskStatus, User
from app.services.deployment_pubsub import pubsub
from app.utils.capabilities import ensure_view_deployment_owner
from app.utils.keycloak_auth import get_current_user_keycloak

router = APIRouter()

# ----------------------------------------------------------------
# LIVE STREAM — Server-Sent Events for progress + log tail
# ----------------------------------------------------------------
#
# Several kinds of events flow through the stream:
#
# * ``event: snapshot`` — fired once at connect with the latest task's
#   current_phase / progress_pct / status. Lets a freshly-loaded page
#   render the bar at the right position before the worker emits its
#   next progress update.
# * ``event: progress`` — every ``task-progress`` from the worker. The
#   payload includes ``phase``, ``phase_index``, ``total_phases``,
#   ``progress_pct``, ``message``.
# * ``event: log`` — every ``task-log`` from the worker. The payload
#   is the LogEntry dict (timestamp, level, category, message, plus
#   tool/streaming flags for streaming subprocess lines).
# * ``event: overflow`` — emitted by the in-process pubsub when a
#   slow consumer overran its bounded queue.
# * comment lines starting with ``:`` are SSE keepalive pings.
#
# The stream stays open until the deployment reaches a terminal state
# (success/failed/cancelled), the client disconnects, or the backend
# shuts down. There's no client-driven close — EventSource handles
# reconnect automatically.
@router.get("/{deployment_id}/stream")
async def stream_deployment_events(
    deployment_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Live progress + log stream for one deployment as Server-Sent Events.

    The connection is authenticated with the same Keycloak dependency
    used elsewhere; the standard auth middleware also vets the token
    before this handler runs. After auth we attach to the in-process
    pubsub for this deployment and forward every event to the client.
    """
    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    # Inspect-only view via capabilities. The live stream surfaces
    # task-log lines (raw worker stdout incl. terraform output, packer
    # build chatter, etc.); course-teachers of the deployment-owner's
    # course are in the inspect set, owners and admins keep their access,
    # and plain members still see metadata only.
    ensure_view_deployment_owner(current_user, deployment, db)

    # Snapshot the latest task once before subscribing so the client
    # gets a meaningful initial state. Reading happens before the
    # generator yields its first chunk to avoid the "subscribed but
    # nothing buffered yet" gap.
    latest_task = crud_deployments.get_latest_task(db, deployment_id)
    snapshot_payload = {
        "task_id": str(latest_task.taskId) if latest_task else None,
        "status": latest_task.status.value if latest_task else None,
        "current_phase": getattr(latest_task, "current_phase", None),
        "progress_pct": getattr(latest_task, "progress_pct", None),
        "type": latest_task.type.value if latest_task else None,
    }
    initial_status = latest_task.status if latest_task else None

    deployment_id_str = str(deployment_id)

    async def event_stream() -> AsyncIterator[bytes]:
        queue = pubsub.subscribe(deployment_id_str)
        try:
            yield _sse_frame("snapshot", snapshot_payload)

            # Backfill what's been happening lately. The pubsub keeps a
            # bounded ring buffer of recent events per deployment so a
            # client connecting mid-stream sees the last few minutes of
            # progress / log output instead of an empty tail until the
            # next worker line lands. Replays the buffer in order so
            # ``streamCurrentPhaseIndex``/``streamProgress`` end up at
            # their latest values before the live loop starts.
            for past_event in pubsub.recent(deployment_id_str):
                event_name = _event_name_for(past_event.get("type"))
                yield _sse_frame(event_name, past_event)

            # If the task is already in a terminal state we still yield
            # the snapshot but close the stream right away — no live
            # events will ever arrive for this deployment.
            if initial_status in (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED):
                return

            # Heartbeat / event-pump loop. Wait up to 15s for an event;
            # if nothing arrives, send a ``: keepalive`` comment so
            # proxies and the EventSource client don't time out.
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue

                event_name = _event_name_for(event.get("type"))
                yield _sse_frame(event_name, event)

                # Stop streaming once the parent task reaches a
                # terminal state. The lifecycle events (succeeded /
                # failed / revoked) flow through the same pubsub key,
                # so we look for them right here. Without this break
                # the connection would dangle until the client closes
                # it.
                if event.get("type") in ("task-succeeded", "task-failed", "task-revoked"):
                    return
        except asyncio.CancelledError:
            # FastAPI cancels the generator on client disconnect.
            raise
        finally:
            pubsub.unsubscribe(deployment_id_str, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx response buffering
            "Connection": "keep-alive",
        },
    )


_EVENT_NAME_MAP: dict[str, str] = {
    "task-progress": "progress",
    "task-log": "log",
    "task-overflow": "overflow",
    "task-started": "started",
    "task-succeeded": "succeeded",
    "task-failed": "failed",
    "task-revoked": "revoked",
}


def _event_name_for(celery_event_type: str | None) -> str:
    """Map Celery event type names onto short SSE event names.

    Frontend code attaches listeners by these short names rather than
    the verbose celery-internal ones; ``_EVENT_NAME_MAP`` is the
    single source of truth on both sides of the wire.
    """
    return _EVENT_NAME_MAP.get(celery_event_type or "", "message")


def _sse_frame(event_name: str, payload: dict) -> bytes:
    """Serialise one SSE frame.

    SSE format:

    ```
    event: <name>\\n
    data: <json>\\n
    \\n
    ```

    Embedded newlines in the JSON would split the frame into multiple
    ``data:`` lines per the SSE spec; we use ``json.dumps`` defaults
    which keep everything on one line.
    """
    body = json.dumps(payload, default=str)
    return f"event: {event_name}\ndata: {body}\n\n".encode()
