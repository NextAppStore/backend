"""The API side of the Postgres job queue: queueing, notifications, finalizer, reaper.

* ``prepare_task_in_tx`` seals the job payload on the row; the commit is
  the hand-over (``task_queued`` is delivered on commit, not before).
* ``task_finished`` fires once per transition into a terminal status.
* The finalizer does a task's follow-up work exactly once, however many API
  processes race for it. Regression for the Celery setup, where every
  uvicorn process ran its own event listener and the access mails went out
  once per process (confirmed with four listeners before the rebuild).
* The reaper fails tasks whose worker stopped renewing its lease.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import timedelta
from unittest.mock import patch

import psycopg
import pytest

from app.database import libpq_url
from app.models import (
    App,
    Deployment,
    Task,
    TaskEvent,
    TaskStatus,
    TaskType,
    TerraformState,
    User,
    UserRole,
)
from app.services import jobs, task_finalizer, task_service
from app.utils.crypto import decrypt, encrypt
from app.utils.time import utcnow
from tests.conftest import _TEST_DB_URL, TestingSessionLocal

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------
# helpers
# ----------------------------------------------------------------
def _deployment(db) -> Deployment:
    user = User(userId=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}@x.de", username="u", role=UserRole.STUDENT)
    app = App(appId=uuid.uuid4(), name="a", userId=user.userId, git_link="https://example.com/r.git")
    dep = Deployment(deploymentId=uuid.uuid4(), name="d", userId=user.userId, appId=app.appId)
    db.add_all([user, app, dep])
    db.commit()
    return dep


def _task(db, dep, *, type_=TaskType.DEPLOY, status=TaskStatus.SUCCESS, **columns) -> Task:
    task = Task(taskId=uuid.uuid4(), deploymentId=dep.deploymentId, type=type_, status=status, **columns)
    if status in jobs.FINISHED_STATUSES:
        task.finished_at = task.finished_at or utcnow()
    db.add(task)
    db.commit()
    return task


def _payload() -> jobs.JobPayload:
    return jobs.JobPayload(
        app_id="app-1",
        app_git_link="https://example.com/r.git",
        release="v1",
        user_vars={"terraform": {"x": "secret-value"}},
        teams={"t": [{"email": "a@x.de"}]},
        openstack_envelope={"encrypted_secret_b64": "abc"},
    )


@pytest.fixture
def listener():
    """A raw LISTEN connection on the test database, as the worker/API use it."""
    conn = psycopg.connect(libpq_url(_TEST_DB_URL), autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def _drain(conn, timeout=0.5) -> list[tuple[str, str]]:
    return [(n.channel, n.payload) for n in conn.notifies(timeout=timeout)]


# ----------------------------------------------------------------
# queueing
# ----------------------------------------------------------------
def test_prepare_task_seals_payload_and_stores_only_the_token_hash(db):
    dep = _deployment(db)
    task = task_service.prepare_task_in_tx(db, dep.deploymentId, TaskType.DEPLOY, _payload())
    db.commit()

    assert task.status == TaskStatus.PENDING
    # Ciphertext on the row; nothing of the variables readable in it.
    assert b"secret-value" not in task.payload
    opened = json.loads(decrypt(task.payload))
    assert opened["user_vars"] == {"terraform": {"x": "secret-value"}}
    assert opened["app_id"] == "app-1"
    # The token travels only inside the sealed payload; the row has its hash.
    assert task.state_token_hash == jobs.state_token_hash(opened["state_token"])


def test_second_active_task_is_refused(db):
    dep = _deployment(db)
    task_service.prepare_task_in_tx(db, dep.deploymentId, TaskType.DEPLOY, _payload())
    db.commit()
    with pytest.raises(task_service.ActiveTaskExistsError):
        task_service.prepare_task_in_tx(db, dep.deploymentId, TaskType.DESTROY, _payload())


def test_task_queued_is_delivered_on_commit_not_before(db, listener):
    listener.execute("LISTEN task_queued")
    dep = _deployment(db)
    _drain(listener, timeout=0.1)

    task_service.prepare_task_in_tx(db, dep.deploymentId, TaskType.DEPLOY, _payload())
    # Flushed, not committed: a worker would not see the row yet, and is
    # not woken yet either.
    assert _drain(listener, timeout=0.3) == []
    db.commit()
    assert _drain(listener) == [("task_queued", "")]


def test_rolled_back_task_never_wakes_the_worker(db, listener):
    listener.execute("LISTEN task_queued")
    dep = _deployment(db)
    task_service.prepare_task_in_tx(db, dep.deploymentId, TaskType.DEPLOY, _payload())
    db.rollback()
    assert _drain(listener) == []


def test_task_finished_fires_only_on_transition_into_a_terminal_status(db, listener):
    listener.execute("LISTEN task_finished")
    dep = _deployment(db)
    task = _task(db, dep, status=TaskStatus.PENDING)

    task.status = TaskStatus.RUNNING
    db.commit()
    assert _drain(listener) == []

    task.status = TaskStatus.SUCCESS
    db.commit()
    assert _drain(listener) == [("task_finished", str(task.taskId))]

    task.logs = "more"  # no status change
    db.commit()
    assert _drain(listener) == []


def test_task_events_notify_carries_the_deployment_id(db, listener):
    listener.execute("LISTEN task_events")
    dep = _deployment(db)
    task = _task(db, dep, status=TaskStatus.RUNNING)
    db.add_all([TaskEvent(taskId=task.taskId, type=jobs.EVENT_LOG, payload="{}") for _ in range(3)])
    db.commit()
    # Postgres folds identical notifications of one transaction into one.
    assert _drain(listener) == [("task_events", str(dep.deploymentId))]


# ----------------------------------------------------------------
# finalizer
# ----------------------------------------------------------------
class _MailCounter:
    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []
        self._lock = threading.Lock()

    def __call__(self, db, deployment_id, terraform_outputs=None):
        with self._lock:
            self.calls.append(deployment_id)


def test_concurrent_finalizers_send_the_deploy_mails_exactly_once(db):
    """Four API processes hear the same task_finished; one does the work."""
    dep = _deployment(db)
    task = _task(db, dep, outputs=json.dumps({"user_accounts": {"value": {}}}))
    task_id = task.taskId  # each "process" has its own session
    mails = _MailCounter()
    start = threading.Barrier(4)
    results: list[bool | BaseException] = []

    def process() -> None:
        try:
            with TestingSessionLocal() as session:
                start.wait()
                results.append(task_finalizer.finalize_task(session, task_id))
        except BaseException as e:  # surfaced by the assertion below
            results.append(e)

    with patch.object(task_finalizer.deployment_notifier, "notify_deployment_succeeded", mails):
        threads = [threading.Thread(target=process) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

    assert sorted(results, key=repr) == [False, False, False, True]
    assert mails.calls == [dep.deploymentId]
    db.refresh(task)
    assert task.finalized_at is not None


def test_finalizer_passes_the_outputs_to_the_notifier(db):
    dep = _deployment(db)
    outputs = {"user_accounts": {"value": {"t": []}}}
    task = _task(db, dep, outputs=json.dumps(outputs))
    with patch.object(task_finalizer.deployment_notifier, "notify_deployment_succeeded") as notify:
        assert task_finalizer.finalize_task(db, task.taskId)
    notify.assert_called_once()
    assert notify.call_args.kwargs["terraform_outputs"] == outputs


def test_failed_deploy_is_finalized_without_mails(db):
    dep = _deployment(db)
    task = _task(db, dep, status=TaskStatus.FAILED)
    with patch.object(task_finalizer.deployment_notifier, "notify_deployment_succeeded") as notify:
        assert task_finalizer.finalize_task(db, task.taskId)
    notify.assert_not_called()


def test_successful_destroy_soft_deletes_and_drops_the_state(db):
    dep = _deployment(db)
    db.add(TerraformState(deploymentId=dep.deploymentId, state=encrypt("{}")))
    task = _task(db, dep, type_=TaskType.DESTROY)

    assert task_finalizer.finalize_task(db, task.taskId)

    db.expire_all()
    assert db.get(Deployment, dep.deploymentId).deleted_at is not None
    assert db.get(TerraformState, dep.deploymentId) is None


def test_failed_destroy_keeps_deployment_and_state(db):
    dep = _deployment(db)
    db.add(TerraformState(deploymentId=dep.deploymentId, state=encrypt("{}")))
    task = _task(db, dep, type_=TaskType.DESTROY, status=TaskStatus.FAILED)

    assert task_finalizer.finalize_task(db, task.taskId)

    db.expire_all()
    assert db.get(Deployment, dep.deploymentId).deleted_at is None
    assert db.get(TerraformState, dep.deploymentId) is not None


def test_finalizing_drops_the_events_of_earlier_tasks_only(db):
    dep = _deployment(db)
    old = _task(db, dep, type_=TaskType.DEPLOY, created_at=utcnow() - timedelta(hours=1))
    new = _task(db, dep, type_=TaskType.PAUSE)
    db.add_all([
        TaskEvent(taskId=old.taskId, type=jobs.EVENT_LOG, payload="{}"),
        TaskEvent(taskId=new.taskId, type=jobs.EVENT_LOG, payload="{}"),
    ])
    db.commit()
    old.finalized_at = utcnow()
    db.commit()

    assert task_finalizer.finalize_task(db, new.taskId)

    remaining = {e.taskId for e in db.query(TaskEvent).all()}
    assert remaining == {new.taskId}


def test_unfinished_or_already_finalized_tasks_are_left_alone(db):
    dep = _deployment(db)
    running = _task(db, dep, status=TaskStatus.RUNNING)
    assert not task_finalizer.finalize_task(db, running.taskId)

    dep2 = _deployment(db)
    done = _task(db, dep2, finalized_at=utcnow())
    with patch.object(task_finalizer.deployment_notifier, "notify_deployment_succeeded") as notify:
        assert not task_finalizer.finalize_task(db, done.taskId)
    notify.assert_not_called()


def test_notifier_failure_does_not_unfinalize_or_retry(db):
    dep = _deployment(db)
    task = _task(db, dep)
    with patch.object(
        task_finalizer.deployment_notifier, "notify_deployment_succeeded", side_effect=RuntimeError("smtp")
    ) as notify:
        assert task_finalizer.finalize_task(db, task.taskId)
        assert task_finalizer.finalize_pending(db) == 0
    assert notify.call_count == 1
    db.refresh(task)
    assert task.finalized_at is not None


def test_finalize_pending_works_off_a_backlog_in_batches(db):
    tasks = [_task(db, _deployment(db), type_=TaskType.PAUSE) for _ in range(task_finalizer.BATCH + 3)]
    assert task_finalizer.finalize_pending(db) == task_finalizer.BATCH
    assert task_finalizer.finalize_pending(db) == 3
    assert task_finalizer.finalize_pending(db) == 0
    db.expire_all()
    assert all(db.get(Task, t.taskId).finalized_at for t in tasks)


# ----------------------------------------------------------------
# reaper
# ----------------------------------------------------------------
def test_reaper_fails_tasks_whose_lease_ran_out(db, listener):
    listener.execute("LISTEN task_finished")
    dep = _deployment(db)
    task = _task(
        db, dep, status=TaskStatus.RUNNING, claimed_by="w-1",
        lease_until=utcnow() - timedelta(seconds=1), logs="partial",
    )

    assert task_finalizer.reap_expired(db) == 1

    db.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.lease_until is None and task.finished_at is not None
    assert task.logs.startswith("partial") and task_finalizer.WORKER_LOST_MESSAGE in task.logs
    [event] = db.query(TaskEvent).filter(TaskEvent.taskId == task.taskId).all()
    assert event.type == jobs.EVENT_FAILED
    assert json.loads(event.payload)["failure_kind"] == jobs.FAILURE_KIND_WORKER_LOST
    # The finalizer is woken like for any other finished task.
    assert ("task_finished", str(task.taskId)) in _drain(listener)


def test_reaper_leaves_live_leases_and_finished_tasks_alone(db):
    live = _task(db, _deployment(db), status=TaskStatus.RUNNING, lease_until=utcnow() + timedelta(minutes=1))
    done = _task(db, _deployment(db), status=TaskStatus.SUCCESS, lease_until=utcnow() - timedelta(minutes=1))
    assert task_finalizer.reap_expired(db) == 0
    db.expire_all()
    assert db.get(Task, live.taskId).status == TaskStatus.RUNNING
    assert db.get(Task, done.taskId).status == TaskStatus.SUCCESS
