"""Unit-Tests für app.utils.lti_auth (Rollen-Mapping, Nonce-Replay, JIT-Sync)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.crud.courses import get_course_by_lti_context_id
from app.models import CourseTeacher, User, UserRole
from app.utils.lti_auth import (
    LTI_CONTEXT_CLAIM,
    LTI_ROLES_CLAIM,
    consume_login_attempt,
    create_login_attempt,
    map_lti_roles_to_app_role,
    sync_user_from_lti,
)

INSTRUCTOR_URN = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"
LEARNER_URN = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"


# ----------------------------------------------------------------
# role mapping
# ----------------------------------------------------------------
@pytest.mark.unit
def test_map_lti_roles_instructor_urn_maps_to_teacher():
    assert map_lti_roles_to_app_role([INSTRUCTOR_URN]) == UserRole.TEACHER


@pytest.mark.unit
def test_map_lti_roles_learner_urn_maps_to_student():
    assert map_lti_roles_to_app_role([LEARNER_URN]) == UserRole.STUDENT


@pytest.mark.unit
def test_map_lti_roles_empty_defaults_to_student():
    assert map_lti_roles_to_app_role([]) == UserRole.STUDENT


@pytest.mark.unit
def test_map_lti_roles_does_not_substring_match():
    """A role string merely containing 'Instructor' must not grant
    elevated access — only the exact IMS URN counts (the throwaway mock's
    ``"Instructor" in r`` check was unsafe)."""
    assert map_lti_roles_to_app_role(["SomeInstructorLookingButUnofficialRole"]) == UserRole.STUDENT


# ----------------------------------------------------------------
# nonce / state replay protection
# ----------------------------------------------------------------
@pytest.mark.unit
def test_login_attempt_is_single_use():
    state, nonce = create_login_attempt()
    assert consume_login_attempt(state, nonce) is True
    assert consume_login_attempt(state, nonce) is False


@pytest.mark.unit
def test_login_attempt_unknown_pair_rejected():
    assert consume_login_attempt("unknown-state", "unknown-nonce") is False


# ----------------------------------------------------------------
# sync_user_from_lti
# ----------------------------------------------------------------
def _claims(*, iss="https://moodle.example", sub="42", email, roles=None, context_id="course-1"):
    return {
        "iss": iss,
        "sub": sub,
        "email": email,
        "name": "Ada Lovelace",
        LTI_ROLES_CLAIM: roles or [LEARNER_URN],
        LTI_CONTEXT_CLAIM: {"id": context_id, "title": "Analytical Engines 101"},
    }


@pytest.mark.integration
def test_sync_user_from_lti_creates_new_user_and_course(db):
    claims = _claims(email="new.student@example.com")
    user = sync_user_from_lti(db, claims)

    assert user.email == "new.student@example.com"
    assert user.lti_iss == "https://moodle.example"
    assert user.lti_sub == "42"
    assert user.role == UserRole.STUDENT
    assert user.courseId is not None

    course = get_course_by_lti_context_id(db, "course-1")
    assert course is not None
    assert course.courseId == user.courseId


@pytest.mark.integration
def test_sync_user_from_lti_repeat_launch_matches_by_lti_identity(db):
    first = sync_user_from_lti(db, _claims(email="repeat@example.com", sub="99"))
    second = sync_user_from_lti(db, _claims(email="repeat@example.com", sub="99"))
    assert first.userId == second.userId


@pytest.mark.integration
def test_sync_user_from_lti_links_existing_keycloak_account_by_email(db):
    """An existing Keycloak-provisioned account with a matching email gets
    the LTI identity attached rather than a duplicate account being made."""
    existing = User(
        keycloak_id="kc-123",
        email="shared@example.com",
        username="shared",
        role=UserRole.STUDENT,
    )
    db.add(existing)
    db.commit()
    db.refresh(existing)

    claims = _claims(email="shared@example.com", sub="77")
    synced = sync_user_from_lti(db, claims)

    assert synced.userId == existing.userId
    assert synced.keycloak_id == "kc-123"
    assert synced.lti_iss == "https://moodle.example"
    assert synced.lti_sub == "77"


@pytest.mark.integration
def test_sync_user_from_lti_instructor_registered_as_course_teacher(db):
    user = sync_user_from_lti(
        db, _claims(email="prof@example.com", roles=[INSTRUCTOR_URN], context_id="course-2")
    )
    assert user.role == UserRole.TEACHER

    link = (
        db.query(CourseTeacher)
        .filter(
            CourseTeacher.courseId == user.courseId,
            CourseTeacher.userId == user.userId,
        )
        .first()
    )
    assert link is not None


@pytest.mark.integration
def test_sync_user_from_lti_rejects_missing_email(db):
    claims = _claims(email=None)
    with pytest.raises(HTTPException) as exc_info:
        sync_user_from_lti(db, claims)
    assert exc_info.value.status_code == 400
