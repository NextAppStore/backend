from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    App,
    AppVersionApproval,
    AppVersionApprovalStatus,
    Course,
    Deployment,
    Team,
    User,
    UserRole,
    UserToDeployment,
    UserToTeam,
)
from app.utils.capabilities import get_my_course_teacher_ids
from app.utils.keycloak_auth import get_current_user_keycloak

router = APIRouter()


class DashboardStatsResponse(BaseModel):
    deployments: int
    apps: int
    courses: int
    # Counts deployments whose owner sits inside one of the requestor's
    # taught courses. 0 for anyone not registered as a course-teacher.
    # Always present so the frontend can render the scope tile without
    # a separate request.
    courseScopeDeployments: int = 0


@router.get("/stats", response_model=DashboardStatsResponse)
def get_dashboard_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """
    Aggregate counts for the dashboard KPI strip.

    Cheap DB-only aggregates — intentionally separated from the OpenStack
    quota call (`GET /quotas/overview`) so the dashboard renders fast even
    when the OpenStack API is slow or unavailable.

    Deployments counter MUST mirror the visibility rules of
    ``GET /deployments`` so the KPI matches what the user actually sees
    on the Deployments page:

      * Teacher/Admin: deployments they own.
      * Student:       deployments they own OR are a team member of OR
                       have a direct ``UserToDeployment`` mapping for.

    Apps counter MUST mirror the role-branched visibility of
    ``GET /apps`` (see ``routers/apps.py``):

      * Admin: every non-deleted app — exactly what
        ``crud_apps.get_apps`` returns when called without
        ``user_id``. Admin keeps the plattform-wide view.
      * Teacher/Student: own apps (regardless of ``is_private`` /
        approval state) OR public apps (``is_private = False``)
        with at least one APPROVED version — mirrors
        ``crud_apps.get_visible_apps``. Teacher gets the student-style
        filter.

    Soft-deleted rows (``deleted_at IS NOT NULL``) are excluded on both
    counters — same as the list endpoints.
    """
    return DashboardStatsResponse(
        deployments=_count_visible_deployments(db, current_user),
        apps=_count_visible_apps(db, current_user),
        courses=db.query(func.count(Course.courseId)).scalar() or 0,
        courseScopeDeployments=_count_course_scope_deployments(db, current_user),
    )


def _count_visible_deployments(db: Session, current_user: User) -> int:
    """Deployments the caller can see on ``GET /deployments``.

    Staff see what they own; students additionally see what they are a
    member of, via a team or a direct mapping. Mirrors
    ``crud_deployments.get_deployments(member_user_id=...)``.
    """
    q = db.query(func.count(Deployment.deploymentId)).filter(
        Deployment.deleted_at.is_(None)
    )
    if current_user.role in (UserRole.TEACHER, UserRole.ADMIN):
        q = q.filter(Deployment.userId == current_user.userId)
    else:
        member_team_ids = db.query(UserToTeam.teamId).filter(
            UserToTeam.userId == current_user.userId
        )
        via_teams = db.query(Team.deploymentId).filter(
            Team.teamId.in_(member_team_ids)
        )
        via_direct = db.query(UserToDeployment.deploymentId).filter(
            UserToDeployment.userId == current_user.userId
        )
        q = q.filter(
            or_(
                Deployment.userId == current_user.userId,
                Deployment.deploymentId.in_(via_teams),
                Deployment.deploymentId.in_(via_direct),
            )
        )
    return q.scalar() or 0


def _count_visible_apps(db: Session, current_user: User) -> int:
    """Apps the caller can see on ``GET /apps``.

    Admins see every non-deleted app (mirrors ``crud_apps.get_apps``);
    everyone else sees their own plus public apps with at least one
    approved version (mirrors ``crud_apps.get_visible_apps``).
    """
    q = db.query(func.count(App.appId)).filter(App.deleted_at.is_(None))
    if current_user.role != UserRole.ADMIN:
        approved_app_ids = (
            db.query(AppVersionApproval.appId)
            .filter(AppVersionApproval.status == AppVersionApprovalStatus.APPROVED)
            .distinct()
            .scalar_subquery()
        )
        q = q.filter(
            or_(
                App.userId == current_user.userId,
                (App.is_private == False)  # noqa: E712
                & App.appId.in_(approved_app_ids),
            )
        )
    return q.scalar() or 0


def _count_course_scope_deployments(db: Session, current_user: User) -> int:
    """Deployments whose owner sits in one of the caller's taught courses.

    0 for anyone not registered as a course-teacher. Soft-deleted rows
    are excluded the same way as the primary counter.
    """
    my_course_ids = get_my_course_teacher_ids(current_user, db)
    if not my_course_ids:
        return 0
    return (
        db.query(func.count(Deployment.deploymentId))
        .join(User, User.userId == Deployment.userId)
        .filter(Deployment.deleted_at.is_(None))
        .filter(User.courseId.in_(my_course_ids))
        .scalar()
    ) or 0
