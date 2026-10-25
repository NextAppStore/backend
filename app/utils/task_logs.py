"""Zerlegt einen gespeicherten Fehlertext in Überschrift und technische Details.

Das Frontend hat diese Logik bisher selbst (``splitTaskLogs`` in
``useDeploymentTasks.ts``) und sucht dafür wörtlich nach den Strings unten.
Das Backend liefert das Ergebnis jetzt zusätzlich fertig in der Task-Antwort
(``failure_headline`` / ``failure_details``), damit das Frontend nicht mehr
auf einen Text-Marker angewiesen ist und der Marker nie übersetzt oder
geändert werden muss.

Die Konstanten bleiben sprachneutral – solange das Frontend noch selbst
parst, dürfen sie sich nicht ändern.
"""

from __future__ import annotations

# Trennzeile zwischen Überschrift und Rohdetails (``celery_event_listener``).
FAILURE_DETAIL_DIVIDER = "--- Technische Details ---"
# Altes Format: ``Task failed: <Einzeiler>\n<Traceback>``.
FAILURE_PREFIX = "Task failed:"


def split_failure_logs(logs: str | None) -> tuple[str | None, str | None]:
    """``(headline, details)`` für einen Fehlertext, sonst ``(None, None)``.

    ``details`` ist ``None``, wenn es keinen technischen Teil gibt.
    """
    if not logs:
        return None, None

    divider = logs.find(FAILURE_DETAIL_DIVIDER)
    if divider >= 0:
        headline = logs[:divider].strip()
        details = logs[divider + len(FAILURE_DETAIL_DIVIDER) :].strip()
        return headline, details or None

    if logs.startswith(FAILURE_PREFIX):
        newline = logs.find("\n")
        if newline > 0:
            return logs[:newline].strip(), logs[newline + 1 :].strip() or None
        return logs.strip(), None

    return None, None
