"""OpenTofu's HTTP state backend under /internal/tfstate.

Replaces the ``pg`` backend in the separate ``postgres-tfstate`` database.
Authentication is per task: task id + the state token from the sealed job
payload, valid only while that task is RUNNING and only for its deployment.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.models import App, Deployment, Task, TaskStatus, TaskType, TerraformState
from app.services import jobs, task_service, tf_state_store
from app.utils.crypto import decrypt, encrypt

pytestmark = pytest.mark.integration

STATE = json.dumps({"version": 4, "serial": 1, "resources": []})


def _running_task(db, owner) -> tuple[Deployment, Task, str]:
    app_row = App(
        appId=uuid.uuid4(), name="a", userId=owner.userId, git_link="https://example.com/r.git"
    )
    dep = Deployment(deploymentId=uuid.uuid4(), name="d", appId=app_row.appId, userId=owner.userId)
    db.add_all([app_row, dep])
    db.flush()
    task = task_service.prepare_task_in_tx(
        db,
        dep.deploymentId,
        TaskType.DEPLOY,
        jobs.JobPayload(
            app_id=str(app_row.appId),
            app_git_link="x",
            release="v1",
            user_vars={},
            teams={},
            openstack_envelope={},
        ),
    )
    token = json.loads(decrypt(task.payload))["state_token"]
    task.status = TaskStatus.RUNNING  # what the worker's claim does
    db.commit()
    return dep, task, token


def test_running_task_can_write_and_read_its_state(unauth_client, db, mock_user):
    dep, task, token = _running_task(db, mock_user)
    auth = (str(task.taskId), token)
    url = f"/internal/tfstate/{dep.deploymentId}"

    assert unauth_client.get(url, auth=auth).status_code == 204  # no state yet
    assert unauth_client.post(url, content=STATE, auth=auth).status_code == 200
    response = unauth_client.get(url, auth=auth)
    assert response.status_code == 200 and response.json()["serial"] == 1

    # Encrypted at rest.
    row = db.get(TerraformState, dep.deploymentId)
    assert b"resources" not in row.state and json.loads(decrypt(row.state))["serial"] == 1


@pytest.mark.parametrize(
    "case", ["no_auth", "wrong_token", "other_deployment", "not_running", "bad_task_id"]
)
def test_state_access_is_refused(unauth_client, db, mock_user, case):
    dep, task, token = _running_task(db, mock_user)
    other_dep, _, _ = _running_task(db, mock_user)
    url = f"/internal/tfstate/{dep.deploymentId}"
    auth = (str(task.taskId), token)
    if case == "no_auth":
        auth = None
    elif case == "wrong_token":
        auth = (str(task.taskId), token + "x")
    elif case == "other_deployment":
        url = f"/internal/tfstate/{other_dep.deploymentId}"
    elif case == "not_running":
        task.status = TaskStatus.SUCCESS
        db.commit()
    elif case == "bad_task_id":
        auth = ("not-a-uuid", token)

    response = unauth_client.get(url, auth=auth)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_locking_follows_opentofus_http_backend(unauth_client, db, mock_user):
    dep, task, token = _running_task(db, mock_user)
    auth = (str(task.taskId), token)
    url = f"/internal/tfstate/{dep.deploymentId}"
    lock_a = json.dumps({"ID": "lock-a", "Who": "tofu"})

    assert unauth_client.request("LOCK", url, content=lock_a, auth=auth).status_code == 200
    # Same lock again: fine. Write with the lock id: fine.
    assert unauth_client.request("LOCK", url, content=lock_a, auth=auth).status_code == 200
    assert unauth_client.post(f"{url}?ID=lock-a", content=STATE, auth=auth).status_code == 200
    assert unauth_client.request("UNLOCK", url, content=lock_a, auth=auth).status_code == 200
    # Lock info without an ID is rejected.
    assert unauth_client.request("LOCK", url, content="{}", auth=auth).status_code == 400


def test_live_lock_of_another_task_blocks(unauth_client, db, mock_user):
    """Another task that is still running keeps its lock (423 on lock/write, 409 on unlock)."""
    dep, task, token = _running_task(db, mock_user)
    other_dep, other, _ = _running_task(db, mock_user)
    db.add(
        TerraformState(
            deploymentId=dep.deploymentId,
            lock_id="theirs",
            lock_info='{"ID": "theirs"}',
            lock_task_id=other.taskId,
        )
    )
    db.commit()
    auth = (str(task.taskId), token)
    url = f"/internal/tfstate/{dep.deploymentId}"

    locked = unauth_client.post(url, content=STATE, auth=auth)
    assert locked.status_code == 423 and locked.json()["ID"] == "theirs"
    assert (
        unauth_client.request(
            "LOCK", url, content=json.dumps({"ID": "mine"}), auth=auth
        ).status_code
        == 423
    )
    assert (
        unauth_client.request("UNLOCK", url, content='{"ID": "mine"}', auth=auth).status_code == 409
    )


def test_task_is_not_locked_out_by_its_own_leftover_lock(unauth_client, db, mock_user):
    """The API was down at the end of an apply: OpenTofu could not unlock.

    The worker then pushes the saved errored.tfstate in the same task under
    a new lock ID; its own leftover lock must not block that.
    """
    dep, task, token = _running_task(db, mock_user)
    auth = (str(task.taskId), token)
    url = f"/internal/tfstate/{dep.deploymentId}"
    assert (
        unauth_client.request(
            "LOCK", url, content=json.dumps({"ID": "apply"}), auth=auth
        ).status_code
        == 200
    )
    # ... unlock never arrived. Now `tofu state push`:
    assert (
        unauth_client.request(
            "LOCK", url, content=json.dumps({"ID": "push"}), auth=auth
        ).status_code
        == 200
    )
    assert unauth_client.post(f"{url}?ID=push", content=STATE, auth=auth).status_code == 200
    assert (
        unauth_client.request(
            "UNLOCK", url, content=json.dumps({"ID": "push"}), auth=auth
        ).status_code
        == 200
    )
    db.expire_all()
    row = db.get(TerraformState, dep.deploymentId)
    assert row.lock_id is None and json.loads(decrypt(row.state))["serial"] == 1


def test_lock_of_a_dead_task_is_taken_over(unauth_client, db, mock_user):
    """A worker that died mid-apply must not block the deployment's state for good."""
    dep, first, token1 = _running_task(db, mock_user)
    url = f"/internal/tfstate/{dep.deploymentId}"
    assert (
        unauth_client.request(
            "LOCK", url, content=json.dumps({"ID": "old"}), auth=(str(first.taskId), token1)
        ).status_code
        == 200
    )
    first.status = TaskStatus.FAILED  # reaped
    db.commit()

    second = task_service.prepare_task_in_tx(
        db,
        dep.deploymentId,
        TaskType.DESTROY,
        jobs.JobPayload(
            app_id="a",
            app_git_link="x",
            release="v1",
            user_vars={},
            teams={},
            openstack_envelope={},
        ),
    )
    token2 = json.loads(decrypt(second.payload))["state_token"]
    second.status = TaskStatus.RUNNING
    db.commit()
    auth2 = (str(second.taskId), token2)
    assert (
        unauth_client.request(
            "LOCK", url, content=json.dumps({"ID": "new"}), auth=auth2
        ).status_code
        == 200
    )
    assert unauth_client.post(f"{url}?ID=new", content=STATE, auth=auth2).status_code == 200


def test_internal_routes_are_not_in_the_openapi_document(unauth_client):
    paths = unauth_client.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith("/internal")]


# ----------------------------------------------------------------
# restoring a state the worker rescued
# ----------------------------------------------------------------
def _state(serial: int, lineage: str = "L1") -> str:
    return json.dumps({"version": 4, "serial": serial, "lineage": lineage, "resources": []})


def test_restore_puts_back_a_newer_rescued_state_and_clears_a_stale_lock(db, mock_user):
    dep, task, _ = _running_task(db, mock_user)
    db.add(
        TerraformState(
            deploymentId=dep.deploymentId,
            state=encrypt(_state(3)),
            lock_id="apply",
            lock_task_id=task.taskId,
        )
    )
    task.status = TaskStatus.FAILED  # the job has ended; its lock is stale
    db.commit()

    assert "serial 4" in tf_state_store.restore(db, dep.deploymentId, _state(4))

    db.expire_all()
    row = db.get(TerraformState, dep.deploymentId)
    assert json.loads(decrypt(row.state))["serial"] == 4 and row.lock_id is None


@pytest.mark.parametrize(
    ("stored", "rescued", "reason"),
    [
        (_state(4), _state(4), "same or newer"),
        (_state(5), _state(4), "same or newer"),
        (_state(3, "L2"), _state(4), "lineage"),
    ],
)
def test_restore_refuses_to_lose_data_without_force(db, mock_user, stored, rescued, reason):
    dep, task, _ = _running_task(db, mock_user)
    task.status = TaskStatus.FAILED
    db.add(TerraformState(deploymentId=dep.deploymentId, state=encrypt(stored)))
    db.commit()
    with pytest.raises(tf_state_store.RestoreRefused, match=reason):
        tf_state_store.restore(db, dep.deploymentId, rescued)
    assert "restored" in tf_state_store.restore(db, dep.deploymentId, rescued, force=True)


def test_restore_refuses_while_a_job_holds_the_lock(db, mock_user):
    dep, task, _ = _running_task(db, mock_user)
    db.add(TerraformState(deploymentId=dep.deploymentId, lock_id="x", lock_task_id=task.taskId))
    db.commit()
    with pytest.raises(tf_state_store.RestoreRefused, match="running task"):
        tf_state_store.restore(db, dep.deploymentId, _state(1), force=True)


def test_restore_rejects_something_that_is_not_a_state(db, mock_user):
    dep, _, _ = _running_task(db, mock_user)
    with pytest.raises(tf_state_store.RestoreRefused, match="not an OpenTofu state"):
        tf_state_store.restore(db, dep.deploymentId, "{}")
