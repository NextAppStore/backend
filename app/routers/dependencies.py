"""Shared FastAPI dependencies for the deployment endpoints.

Eleven deployment endpoints opened with the same three steps: load the
row by path parameter, 404 if it is gone, then run an access guard.
Written out per endpoint that is 5 lines of boilerplate each, and — the
reason this module exists — an access check that a new endpoint can
simply forget to write. As a dependency it is part of the signature, so
an endpoint either declares which guard it wants or it does not compile
into a route at all.

Usage::

    @router.post("/{deployment_id}/pause")
    def pause_deployment(
        deployment: Deployment = Depends(require_operate_deployment_detail),
        ...
    ):

The path parameter stays declared (on the dependency rather than the
handler), so the generated OpenAPI is unchanged.
"""

from collections.abc import Callable
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.crud import deployments as crud_deployments
from app.database import get_db
from app.models import Deployment, User
from app.utils.capabilities import (
    ensure_operate_deployment,
    ensure_resend_access,
    ensure_view_deployment_owner,
)
from app.utils.keycloak_auth import get_current_user_keycloak
from app.utils.permissions import ensure_deployment_access

# One spelling per resource, so the 404 body cannot drift between
# endpoints that are all talking about the same missing row. The strings
# are part of the API surface — the frontend matches on them — which is
# exactly why they should exist once.
DEPLOYMENT_NOT_FOUND = "Deployment not found"
APP_NOT_FOUND = "App not found"
COURSE_NOT_FOUND = "Course not found"


def _deployment_dependency(
    guard: Callable[[User, Deployment, Session], None],
    *,
    with_details: bool = False,
):
    """Build a dependency that loads a deployment and applies ``guard``.

    ``with_details`` picks the eager-loading query for the endpoints that
    go on to read relations (teams, app, user); the others take the plain
    row so they don't pay for joins they never touch.

    ``guard`` is called as ``guard(user, deployment, db)``. Guards whose
    own signature differs are adapted at the call site below, so this
    factory only ever knows one shape.
    """
    def _dep(
        deployment_id: UUID,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user_keycloak),
    ) -> Deployment:
        load = (
            crud_deployments.get_deployment_with_details
            if with_details
            else crud_deployments.get_deployment
        )
        deployment = load(db, deployment_id)
        if not deployment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=DEPLOYMENT_NOT_FOUND,
            )
        guard(current_user, deployment, db)
        return deployment

    return _dep


def _access_guard(user: User, deployment: Deployment, db: Session) -> None:
    """``ensure_deployment_access`` takes its arguments the other way round."""
    ensure_deployment_access(deployment, user, db)


def _own_access_guard(user: User, deployment: Deployment, db: Session) -> None:
    """Resend/read of one's OWN access mail — target is always the caller."""
    ensure_resend_access(user, deployment, user.userId, db)


# Owner view: owner, admin, or course-teacher of the owner's course.
# Gates logs, outputs, raw state and uploaded file bytes.
require_deployment_owner_view = _deployment_dependency(ensure_view_deployment_owner)

# Operate: everything that dispatches a worker task (destroy, pause,
# resume, per-VM redeploy).
require_operate_deployment = _deployment_dependency(ensure_operate_deployment)
require_operate_deployment_detail = _deployment_dependency(
    ensure_operate_deployment, with_details=True
)

# Member-level read: owner view PLUS the members of the deployment.
require_deployment_access = _deployment_dependency(_access_guard)
require_deployment_access_detail = _deployment_dependency(
    _access_guard, with_details=True
)

# Self-service access credentials.
require_own_access = _deployment_dependency(_own_access_guard)
