"""Fehlertext-Zerlegung für die API und Sprach-Header beim Celery-Dispatch."""

import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models import TaskStatus, TaskType
from app.schemas import TaskResponse
from app.services import task_service
from app.services.celery_event_listener import _apply_infra_failure
from app.utils.task_logs import (
    FAILURE_DETAIL_DIVIDER,
    FAILURE_PREFIX,
    split_failure_logs,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# split_failure_logs
# ---------------------------------------------------------------------------
def test_divider_format_is_split_into_headline_and_details():
    logs = f"Der Worker-Prozess wurde beendet.\n\n{FAILURE_DETAIL_DIVIDER}\nWorkerLostError('x')"
    assert split_failure_logs(logs) == (
        "Der Worker-Prozess wurde beendet.",
        "WorkerLostError('x')",
    )


def test_legacy_prefix_takes_first_line_as_headline():
    assert split_failure_logs(f"{FAILURE_PREFIX} boom\nTraceback...\n  line") == (
        "Task failed: boom",
        "Traceback...\n  line",
    )
    assert split_failure_logs(f"{FAILURE_PREFIX} boom") == ("Task failed: boom", None)


@pytest.mark.parametrize("logs", [None, "", '[{"level": "INFO", "message": "ok"}]', "just text"])
def test_non_failure_logs_yield_nothing(logs):
    assert split_failure_logs(logs) == (None, None)


def test_divider_without_details():
    assert split_failure_logs(f"Kopfzeile\n{FAILURE_DETAIL_DIVIDER}\n") == ("Kopfzeile", None)


def test_infra_failure_written_by_the_listener_round_trips_in_both_languages():
    """Was der Listener schreibt, muss ``split_failure_logs`` wieder zerlegen können."""
    for locale in ("de", "en"):
        update: dict = {}
        _apply_infra_failure(update, "WorkerLostError('x')", "tb", locale)
        headline, details = split_failure_logs(update["logs"])
        assert headline and "WorkerLostError" in details

    update = {}
    _apply_infra_failure(update, "SomethingElse('x')", "tb", "en")
    assert split_failure_logs(update["logs"])[0].startswith(FAILURE_PREFIX)


# ---------------------------------------------------------------------------
# TaskResponse
# ---------------------------------------------------------------------------
def _task(logs):
    return SimpleNamespace(
        taskId=uuid.uuid4(),
        deploymentId=uuid.uuid4(),
        celeryTaskId="c",
        type=TaskType.DEPLOY,
        status=TaskStatus.FAILED,
        started_at=None,
        finished_at=None,
        logs=logs,
        tf_state=None,
        outputs=None,
        current_phase=None,
        progress_pct=None,
        created_at=datetime(2026, 1, 1),
    )


def test_task_response_exposes_failure_fields():
    body = TaskResponse.model_validate(
        _task(f"Kopfzeile\n{FAILURE_DETAIL_DIVIDER}\nraw")
    ).model_dump()
    assert body["failure_headline"] == "Kopfzeile"
    assert body["failure_details"] == "raw"
    # ``logs`` bleibt unverändert, bestehende Clients sehen nichts anderes.
    assert body["logs"].startswith("Kopfzeile")


def test_task_response_failure_fields_are_none_for_normal_logs():
    body = TaskResponse.model_validate(_task('[{"message": "ok"}]')).model_dump()
    assert body["failure_headline"] is None
    assert body["failure_details"] is None


# ---------------------------------------------------------------------------
# Celery-Dispatch: Sprache als Header
# ---------------------------------------------------------------------------
def _dispatch(locale):
    task = SimpleNamespace(
        taskId=uuid.uuid4(), deploymentId=uuid.uuid4(), celeryTaskId=None, locale=locale
    )
    with patch("app.services.task_service.celery_app.send_task") as send_task:
        send_task.return_value = SimpleNamespace(id="celery-1")
        task_service.dispatch_to_celery(MagicMock(), task, "tasks.deploy_application", ["a", "b"])
    return send_task


@pytest.mark.parametrize("locale", ["de", "en"])
def test_locale_is_sent_as_message_header(locale):
    send_task = _dispatch(locale)
    send_task.assert_called_once()
    assert send_task.call_args.kwargs["headers"] == {"locale": locale}


def test_positional_arguments_are_not_changed():
    """Ein Worker ohne Sprachunterstützung bekommt weiterhin genau seine Argumente."""
    send_task = _dispatch("en")
    assert send_task.call_args.args == ("tasks.deploy_application",)
    assert send_task.call_args.kwargs["args"] == ["a", "b"]
