"""Sprachwahl über ``Accept-Language`` gegen die echte App (Postgres)."""

import json
import uuid

import pytest

from app.i18n.locale import current_locale
from app.models import App, Deployment, Task, TaskType
from app.services import jobs, task_service


def _payload() -> jobs.JobPayload:
    return jobs.JobPayload(
        app_id="app-1",
        app_git_link="https://example.com/r.git",
        release="v1",
        user_vars={},
        teams={},
        openstack_envelope={"encrypted_secret_b64": "abc"},
    )


def _unknown_app(admin_client, accept_language):
    headers = {} if accept_language is None else {"Accept-Language": accept_language}
    return admin_client.put(f"/admin/apps/{uuid.uuid4()}", headers=headers)


@pytest.mark.integration
@pytest.mark.parametrize(
    "accept_language, expected",
    [
        ("de", "App nicht gefunden"),
        ("de-DE,de;q=0.9,en;q=0.8", "App nicht gefunden"),
        ("en", "App not found"),
        ("en-US,en;q=0.9", "App not found"),
        ("fr", "App nicht gefunden"),  # unbekannt → Standardsprache
        ("", "App nicht gefunden"),
    ],
)
def test_error_detail_follows_accept_language(admin_client, accept_language, expected):
    response = _unknown_app(admin_client, accept_language)
    assert response.status_code == 404
    assert response.json()["detail"] == expected


@pytest.mark.integration
def test_response_carries_content_language(admin_client):
    assert _unknown_app(admin_client, "en").headers["content-language"] == "en"
    assert _unknown_app(admin_client, "de").headers["content-language"] == "de"


@pytest.mark.integration
def test_successful_responses_also_carry_content_language(client):
    response = client.get("/health", headers={"Accept-Language": "de"})
    assert response.status_code == 200
    assert response.headers["content-language"] == "de"


@pytest.mark.integration
def test_forbidden_detail_keeps_its_code_untranslated(student_client):
    """Strukturierte Fehler (``code``/``reason``) bleiben sprachneutral."""
    response = student_client.get(
        "/admin/apps/versions/pending", headers={"Accept-Language": "de"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == {"code": "role_required", "required": ["admin"]}


def _deployment(db, user):
    app = App(
        appId=uuid.uuid4(),
        name=f"app-{uuid.uuid4().hex[:8]}",
        userId=user.userId,
        git_link="https://example.com/repo.git",
    )
    db.add(app)
    db.flush()
    deployment = Deployment(
        deploymentId=uuid.uuid4(),
        name=f"d-{uuid.uuid4().hex[:8]}",
        appId=app.appId,
        userId=user.userId,
        releaseTag="v1.0.0",
        userInputVar=json.dumps({"terraform": {}, "packer": {}}),
    )
    db.add(deployment)
    db.flush()
    return deployment


@pytest.mark.integration
@pytest.mark.parametrize("locale, expected", [("en", "en"), ("de", "de")])
def test_task_remembers_the_language_of_the_triggering_request(db, mock_user, locale, expected):
    """Mails und Reconciler-Logs laufen ohne Request und brauchen die Sprache am Task."""
    deployment = _deployment(db, mock_user)
    token = current_locale.set(locale)
    try:
        task = task_service.prepare_task_in_tx(
            db, deployment.deploymentId, TaskType.DEPLOY, _payload()
        )
    finally:
        current_locale.reset(token)
    db.commit()
    assert db.query(Task).filter(Task.taskId == task.taskId).one().locale == expected


@pytest.mark.integration
def test_task_defaults_to_german_outside_of_a_request(db, mock_user):
    deployment = _deployment(db, mock_user)
    task = task_service.prepare_task_in_tx(
        db, deployment.deploymentId, TaskType.DEPLOY, _payload()
    )
    db.commit()
    assert task.locale == "de"


@pytest.mark.integration
def test_missing_bearer_token_is_translated(unauth_client):
    """``HTTPBearer`` wirft den Text selbst – der Handler übersetzt ihn trotzdem."""
    for header, expected in [("de", "Nicht authentifiziert"), ("en", "Not authenticated")]:
        response = unauth_client.get("/users/me", headers={"Accept-Language": header})
        assert response.status_code in (401, 403)
        assert response.json()["detail"] == expected
