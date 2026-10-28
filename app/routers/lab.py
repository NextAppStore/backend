"""Kubernetes-App student-pod management (backend#37).

``labctl`` (``Kubernetes-App/packer/files/labctl``) ported onto the HTTP
API and the Postgres task queue:

* ``GET .../students`` reads the cluster directly and returns immediately
  — it is a cheap read and should feel instant in the UI.
* Every mutating action (reassign/reset/add/remove-student, passwd)
  enqueues a ``Task`` and returns 202, exactly like pause/resume/redeploy
  on ``app.routers.deployments`` — the worker runs the actual Kubernetes
  calls, never this process (``AGENTS.md`` ``§5``: no blocking cloud calls
  in HTTP endpoints).
* ``GET .../kubeconfig`` is a teacher-only download of the team's
  cluster-admin kubeconfig (IPv4-patched), gated at the stricter
  "operate" level since the token it carries is cluster-admin.

A lab action always needs an explicit team (kubeconfigs are per-team, not
per-deployment — a deployment can have several teams, each its own
single-VM k3s cluster), so every route is nested under
``/deployments/{deployment_id}/lab/teams/{team_id}``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy.orm import Session

from app.crud import deployments as crud_deployments
from app.crud import locks as crud_locks
from app.crud import teams as crud_teams
from app.database import get_db
from app.models import Deployment, Team, TaskType, User
from app.schemas import (
    AddStudentRequest,
    LabActionAccepted,
    PasswdStudentRequest,
    ResetStudentRequest,
    StudentStatusResponse,
    LAB_USERNAME_PATTERN,
)
from app.services import k8s_client
from app.services import task_service as task_service_module
from app.services.jobs import JobPayload
from app.utils.capabilities import ensure_operate_lab, ensure_view_lab
from app.utils.permissions import get_current_user as get_current_user_keycloak

router = APIRouter()

# Path params aren't validated by a body schema's regex — redeclare labctl's
# username contract here so a malformed path segment 422s before it reaches
# the queue (same pattern as ``AddStudentRequest.username``).
_UsernamePath = Path(..., pattern=LAB_USERNAME_PATTERN)


def _load_deployment_and_team(db: Session, deployment_id: UUID, team_id: UUID) -> tuple[Deployment, Team]:
    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    team = crud_teams.get_team(db, team_id)
    if not team or team.deploymentId != deployment.deploymentId:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    return deployment, team


def _kubeconfig_and_ip(db: Session, deployment_id: UUID, team_name: str) -> tuple[str, str]:
    """The team's raw kubeconfig and its VM's IPv4, or 412 if not deployed yet."""
    kubeconfig = crud_deployments.get_team_kubeconfig(db, deployment_id, team_name)
    fixed_ip = crud_deployments.get_team_fixed_ip_v4(db, deployment_id, team_name)
    if not kubeconfig or not fixed_ip:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={"reason": "lab_not_deployed"},
        )
    return kubeconfig, fixed_ip


def _queue_lab_task(db: Session, deployment_id: UUID, task_type: TaskType, payload: JobPayload) -> LabActionAccepted:
    crud_locks.acquire_deployment_xact_lock(db, deployment_id)
    try:
        task = task_service_module.prepare_task_in_tx(db, deployment_id=deployment_id, task_type=task_type, payload=payload)
    except task_service_module.ActiveTaskExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deployment already has an active task",
        )
    db.commit()
    db.refresh(task)
    return LabActionAccepted(task_id=task.taskId, status="pending")


@router.get("/{deployment_id}/lab/teams/{team_id}/students", response_model=list[StudentStatusResponse])
def list_students(
    deployment_id: UUID,
    team_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """All students of one team with status, port and generation (``labctl list``)."""
    deployment, team = _load_deployment_and_team(db, deployment_id, team_id)
    ensure_view_lab(current_user, deployment, db)
    kubeconfig, fixed_ip = _kubeconfig_and_ip(db, deployment_id, team.name)
    with k8s_client.team_api_client(kubeconfig, fixed_ip) as api:
        return k8s_client.list_students(api)


@router.get("/{deployment_id}/lab/teams/{team_id}/kubeconfig")
def download_kubeconfig(
    deployment_id: UUID,
    team_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Download the team's cluster-admin kubeconfig (IPv4-patched).

    Gated at ``ensure_operate_lab``, not the weaker view gate: this token
    is cluster-admin on the team's VM, stricter than read-only pod status.
    """
    deployment, team = _load_deployment_and_team(db, deployment_id, team_id)
    ensure_operate_lab(current_user, deployment, db)
    kubeconfig, fixed_ip = _kubeconfig_and_ip(db, deployment_id, team.name)
    patched = k8s_client.patch_server_to_ipv4(kubeconfig, fixed_ip)
    filename = f"{team.name}.kubeconfig"
    return Response(
        content=patched,
        media_type="application/yaml",
        headers={
            "Content-Disposition": (f'attachment; filename="{filename}"; filename*=UTF-8\'\'{filename}'),
        },
    )


@router.post(
    "/{deployment_id}/lab/teams/{team_id}/students",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=LabActionAccepted,
)
def add_student(
    deployment_id: UUID,
    team_id: UUID,
    body: AddStudentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Create a new student pod (``labctl add-student``)."""
    deployment, team = _load_deployment_and_team(db, deployment_id, team_id)
    ensure_operate_lab(current_user, deployment, db)
    kubeconfig, fixed_ip = _kubeconfig_and_ip(db, deployment_id, team.name)
    payload = JobPayload(
        team=team.name,
        student_username=body.username,
        student_password=body.password,
        node_port=body.port,
        kubeconfig=k8s_client.patch_server_to_ipv4(kubeconfig, fixed_ip),
    )
    return _queue_lab_task(db, deployment_id, TaskType.LAB_ADD_STUDENT, payload)


@router.post(
    "/{deployment_id}/lab/teams/{team_id}/students/{username}/reassign",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=LabActionAccepted,
)
def reassign_student(
    deployment_id: UUID,
    team_id: UUID,
    username: str = _UsernamePath,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Assign a new pod to a student; home directory is kept (``labctl reassign``)."""
    deployment, team = _load_deployment_and_team(db, deployment_id, team_id)
    ensure_operate_lab(current_user, deployment, db)
    kubeconfig, fixed_ip = _kubeconfig_and_ip(db, deployment_id, team.name)
    payload = JobPayload(
        team=team.name,
        student_username=username,
        kubeconfig=k8s_client.patch_server_to_ipv4(kubeconfig, fixed_ip),
    )
    return _queue_lab_task(db, deployment_id, TaskType.LAB_REASSIGN, payload)


@router.post(
    "/{deployment_id}/lab/teams/{team_id}/students/{username}/reset",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=LabActionAccepted,
)
def reset_student(
    deployment_id: UUID,
    team_id: UUID,
    body: ResetStudentRequest,
    username: str = _UsernamePath,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """New pod and empty home directory for a student (``labctl reset``)."""
    deployment, team = _load_deployment_and_team(db, deployment_id, team_id)
    ensure_operate_lab(current_user, deployment, db)
    kubeconfig, fixed_ip = _kubeconfig_and_ip(db, deployment_id, team.name)
    payload = JobPayload(
        team=team.name,
        student_username=username,
        new_password_requested=body.new_password,
        kubeconfig=k8s_client.patch_server_to_ipv4(kubeconfig, fixed_ip),
    )
    return _queue_lab_task(db, deployment_id, TaskType.LAB_RESET, payload)


@router.post(
    "/{deployment_id}/lab/teams/{team_id}/students/{username}/passwd",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=LabActionAccepted,
)
def change_student_password(
    deployment_id: UUID,
    team_id: UUID,
    body: PasswdStudentRequest,
    username: str = _UsernamePath,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Rotate a student's password, then reassign (``labctl passwd``).

    Not its own task type: the worker's ``LAB_REASSIGN`` job rotates the
    secret first when ``student_password``/a random one is requested,
    mirroring ``labctl``'s own ``cmd_passwd`` -> ``cmd_reassign`` call.
    """
    deployment, team = _load_deployment_and_team(db, deployment_id, team_id)
    ensure_operate_lab(current_user, deployment, db)
    kubeconfig, fixed_ip = _kubeconfig_and_ip(db, deployment_id, team.name)
    payload = JobPayload(
        team=team.name,
        student_username=username,
        new_password_requested=True,
        student_password=body.password,
        kubeconfig=k8s_client.patch_server_to_ipv4(kubeconfig, fixed_ip),
    )
    return _queue_lab_task(db, deployment_id, TaskType.LAB_REASSIGN, payload)


@router.delete(
    "/{deployment_id}/lab/teams/{team_id}/students/{username}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=LabActionAccepted,
)
def remove_student(
    deployment_id: UUID,
    team_id: UUID,
    username: str = _UsernamePath,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Delete a student's pod, service, secret and home directory (``labctl remove-student``)."""
    deployment, team = _load_deployment_and_team(db, deployment_id, team_id)
    ensure_operate_lab(current_user, deployment, db)
    kubeconfig, fixed_ip = _kubeconfig_and_ip(db, deployment_id, team.name)
    payload = JobPayload(
        team=team.name,
        student_username=username,
        kubeconfig=k8s_client.patch_server_to_ipv4(kubeconfig, fixed_ip),
    )
    return _queue_lab_task(db, deployment_id, TaskType.LAB_REMOVE_STUDENT, payload)
