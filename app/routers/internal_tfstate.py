"""OpenTofu's HTTP state backend.

The worker points OpenTofu's ``http`` backend here for every job; OpenTofu
then reads, writes and locks the deployment's state itself. Credentials are
HTTP Basic: the task ID as user name and the task's state token as password
(see ``services/tf_state_store``). That replaces the ``pg`` backend in a
separate database, whose connection string every job had to carry.

Not part of the public API: left out of the OpenAPI document, and the
nginx in front of staging/prod does not route ``/api/internal/``; the
worker reaches the backend directly on the container network. The token
check stands on its own either way.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Task
from app.services import tf_state_store

router = APIRouter(include_in_schema=False)

_basic = HTTPBasic(auto_error=False)


def _task(
    deployment_id: uuid.UUID,
    credentials: HTTPBasicCredentials | None = Depends(_basic),
    db: Session = Depends(get_db),
) -> Task:
    """Authenticate a state request: the running task of ``deployment_id`` whose token matches.

    Raises 401 (with a Basic challenge, so OpenTofu retries with
    credentials) when the credentials are missing, the task is not
    running, or it belongs to another deployment.
    """
    task = None
    if credentials is not None:
        task = tf_state_store.authorize(db, deployment_id, credentials.username, credentials.password)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": 'Basic realm="tfstate"'},
        )
    return task


def _locked(e: tf_state_store.LockConflict) -> Response:
    """423 Locked response carrying the current holder's lock info."""
    # OpenTofu reads the body as the competing lock's info and shows it.
    return Response(
        status_code=status.HTTP_423_LOCKED,
        content=e.lock_info or "{}",
        media_type="application/json",
    )


@router.get("/{deployment_id}")
def get_state(deployment_id: uuid.UUID, _: Task = Depends(_task), db: Session = Depends(get_db)) -> Response:
    """Return the deployment's state as JSON, or 204 when none exists yet."""
    state = tf_state_store.read(db, deployment_id)
    if state is None:
        # OpenTofu treats 204 as "no state yet".
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return Response(content=state, media_type="application/json")


@router.post("/{deployment_id}")
async def put_state(
    deployment_id: uuid.UUID,
    request: Request,
    task: Task = Depends(_task),
    db: Session = Depends(get_db),
) -> Response:
    """Store a new state (OpenTofu's http backend writes with POST).

    OpenTofu passes its lock ID as the ``ID`` query parameter; while another
    task holds a live lock the write is refused with 423.
    """
    body = (await request.body()).decode("utf-8")
    try:
        tf_state_store.write(db, deployment_id, task, body, request.query_params.get("ID"))
    except tf_state_store.LockConflict as e:
        return _locked(e)
    return Response(status_code=status.HTTP_200_OK)


@router.delete("/{deployment_id}")
def delete_state(deployment_id: uuid.UUID, _: Task = Depends(_task), db: Session = Depends(get_db)) -> Response:
    """Delete the deployment's state (and its lock)."""
    tf_state_store.drop(db, deployment_id)
    db.commit()
    return Response(status_code=status.HTTP_200_OK)


@router.api_route("/{deployment_id}", methods=["LOCK"])
async def lock_state(
    deployment_id: uuid.UUID,
    request: Request,
    task: Task = Depends(_task),
    db: Session = Depends(get_db),
) -> Response:
    """Take the state lock for the calling task (custom ``LOCK`` method).

    The body is OpenTofu's lock info JSON; 400 when it has no ``ID``, 423
    with the holder's lock info while another running task holds the lock.
    A lock whose holder task is no longer running is stale and taken over.
    """
    body = (await request.body()).decode("utf-8")
    try:
        tf_state_store.lock(db, deployment_id, task, body)
    except tf_state_store.LockConflict as e:
        return _locked(e)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return Response(status_code=status.HTTP_200_OK)


@router.api_route("/{deployment_id}", methods=["UNLOCK"])
async def unlock_state(
    deployment_id: uuid.UUID,
    request: Request,
    task: Task = Depends(_task),
    db: Session = Depends(get_db),
) -> Response:
    """Release the state lock (custom ``UNLOCK`` method).

    An empty body releases unconditionally (force-unlock). 409 with the
    holder's lock info when the body names a different, live lock of
    another task.
    """
    body = (await request.body()).decode("utf-8")
    try:
        tf_state_store.unlock(db, deployment_id, task, body or None)
    except tf_state_store.LockConflict as e:
        return Response(
            status_code=status.HTTP_409_CONFLICT,
            content=e.lock_info or "{}",
            media_type="application/json",
        )
    return Response(status_code=status.HTTP_200_OK)
