"""Fehlertext-Zerlegung für die API."""

import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models import TaskStatus, TaskType
from app.schemas import TaskResponse
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


# ---------------------------------------------------------------------------
# TaskResponse
# ---------------------------------------------------------------------------
def _task(logs):
    return SimpleNamespace(
        taskId=uuid.uuid4(),
        deploymentId=uuid.uuid4(),
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
