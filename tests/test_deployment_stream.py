"""The live stream reads a task's events from ``task_events`` and is woken by NOTIFY.

The first group replays events that are already stored (no listener
needed). The last test runs the real lifespan — one LISTEN connection,
started like in production — and writes events while the stream is open:
the stream must deliver them and end, which it can only do if the
``task_events`` notification woke it (there is no polling to fall back on).
"""

from __future__ import annotations

import json
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import App, Deployment, Task, TaskEvent, TaskStatus, TaskType
from app.services.db_events import dispatcher
from app.utils.permissions import get_current_user as get_current_user_keycloak
from tests.conftest import TestingSessionLocal

pytestmark = pytest.mark.integration


def _deployment_with_events(db, owner, events, status=TaskStatus.RUNNING) -> tuple[Deployment, Task, list[int]]:
    app_row = App(appId=uuid.uuid4(), name="a", userId=owner.userId, git_link="https://example.com/r.git")
    dep = Deployment(deploymentId=uuid.uuid4(), name="d", appId=app_row.appId, userId=owner.userId)
    db.add_all([app_row, dep])
    db.flush()
    task = Task(deploymentId=dep.deploymentId, type=TaskType.PAUSE, status=status)
    db.add(task)
    db.flush()
    rows = [TaskEvent(taskId=task.taskId, type=t, payload=json.dumps({"type": t, **p})) for t, p in events]
    db.add_all(rows)
    db.commit()
    return dep, task, [r.id for r in rows]


def _frames(body: str) -> list[dict]:
    frames = []
    for block in body.strip().split("\n\n"):
        frame: dict = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            frame[key] = value
        if frame:
            frames.append(frame)
    return frames


def test_stream_replays_stored_events_and_ends_with_the_terminal_one(client, db, mock_user):
    dep, _, ids = _deployment_with_events(
        db,
        mock_user,
        [
            ("task-progress", {"phase": "GIT_CLONE", "progress_pct": 10}),
            ("task-log", {"message": "Cloning"}),
            ("task-succeeded", {"status": "success"}),
        ],
    )

    with client.stream("GET", f"/deployments/{dep.deploymentId}/stream") as response:
        frames = _frames(response.read().decode())

    assert [f["event"] for f in frames] == ["snapshot", "progress", "log", "succeeded"]
    assert [int(f["id"]) for f in frames[1:]] == ids
    assert json.loads(frames[2]["data"])["message"] == "Cloning"
    # Same payload shape as before: the stored dict, unchanged.
    assert json.loads(frames[1]["data"]) == {"type": "task-progress", "phase": "GIT_CLONE", "progress_pct": 10}


def test_reconnect_resumes_after_last_event_id(client, db, mock_user):
    dep, _, ids = _deployment_with_events(
        db,
        mock_user,
        [("task-log", {"message": "one"}), ("task-log", {"message": "two"}), ("task-failed", {"status": "failed"})],
        status=TaskStatus.FAILED,
    )

    with client.stream(
        "GET", f"/deployments/{dep.deploymentId}/stream", headers={"Last-Event-ID": str(ids[0])}
    ) as response:
        frames = _frames(response.read().decode())

    assert [f["event"] for f in frames] == ["snapshot", "log", "failed"]
    assert json.loads(frames[1]["data"])["message"] == "two"


def test_finished_task_without_last_event_id_sends_only_the_snapshot(client, db, mock_user):
    """Unchanged behaviour for the frontend: opening a finished task's
    stream shows the snapshot; the full log is in the task row."""
    dep, _, _ = _deployment_with_events(
        db, mock_user, [("task-log", {"message": "x"}), ("task-succeeded", {})], status=TaskStatus.SUCCESS
    )
    with client.stream("GET", f"/deployments/{dep.deploymentId}/stream") as response:
        frames = _frames(response.read().decode())
    assert [f["event"] for f in frames] == ["snapshot"]
    assert json.loads(frames[0]["data"])["status"] == "success"


def test_task_that_finished_without_terminal_event_still_ends_the_stream(client, db, mock_user):
    dep, task, ids = _deployment_with_events(db, mock_user, [("task-log", {"message": "only"})])
    task.status = TaskStatus.FAILED
    db.commit()
    with client.stream(
        "GET", f"/deployments/{dep.deploymentId}/stream", headers={"Last-Event-ID": "0"}
    ) as response:
        frames = _frames(response.read().decode())
    assert [f["event"] for f in frames] == ["snapshot", "log"]


def test_deployment_without_tasks_sends_only_the_snapshot(client, db, mock_user):
    app_row = App(appId=uuid.uuid4(), name="a", userId=mock_user.userId)
    dep = Deployment(deploymentId=uuid.uuid4(), name="d", appId=app_row.appId, userId=mock_user.userId)
    db.add_all([app_row, dep])
    db.commit()
    with client.stream("GET", f"/deployments/{dep.deploymentId}/stream") as response:
        frames = _frames(response.read().decode())
    assert [f["event"] for f in frames] == ["snapshot"]


@pytest.mark.timeout(30)
def test_open_stream_is_woken_by_task_events_notifications(db, mock_user, monkeypatch):
    """Real lifespan: the LISTEN connection wakes the open stream."""
    dep, task, _ = _deployment_with_events(db, mock_user, [("task-log", {"message": "before"})])
    task_id = task.taskId
    deployment_key = str(dep.deploymentId)

    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setenv("DISABLE_BACKGROUND_TASKS", "0")
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_keycloak] = lambda: mock_user

    def worker_writes() -> None:
        # Wait until the stream has read the backlog and is waiting for a
        # wake-up, so what follows can only reach it through NOTIFY.
        deadline = time.monotonic() + 10
        while deployment_key not in dispatcher._waiters and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)
        # What the worker does while the stream is open: a few events, then
        # the result with the terminal event in one transaction.
        with TestingSessionLocal() as session:
            for i in range(3):
                session.add(TaskEvent(taskId=task_id, type="task-log", payload=json.dumps({"message": f"live {i}"})))
                session.commit()
                time.sleep(0.1)
            session.get(Task, task_id).status = TaskStatus.SUCCESS
            session.add(TaskEvent(taskId=task_id, type="task-succeeded", payload=json.dumps({"status": "success"})))
            session.commit()

    try:
        with TestClient(app) as live_client:
            deadline = time.monotonic() + 10
            while not dispatcher.connected and time.monotonic() < deadline:
                time.sleep(0.05)
            assert dispatcher.connected

            # The test client only returns once the response is complete,
            # so the "worker" runs in a thread started beforehand.
            writer = threading.Thread(target=worker_writes)
            writer.start()
            started = time.monotonic()
            with live_client.stream("GET", f"/deployments/{dep.deploymentId}/stream") as response:
                frames = _frames(response.read().decode())
            elapsed = time.monotonic() - started
            writer.join(timeout=5)
    finally:
        app.dependency_overrides.clear()

    assert [json.loads(f["data"]).get("message") for f in frames if f["event"] == "log"] == [
        "before",
        "live 0",
        "live 1",
        "live 2",
    ]
    assert frames[-1]["event"] == "succeeded"
    # Woken by NOTIFY, not by the 15 s keepalive timeout.
    assert elapsed < 10
