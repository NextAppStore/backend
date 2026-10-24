"""Übersetzung von Katalog-Einträgen und vorhandenen Fehlertexten."""

from __future__ import annotations

import logging
import re
import string
from functools import lru_cache
from typing import Any

from app.i18n.catalog import MESSAGES
from app.i18n.locale import DEFAULT_LOCALE, Locale, get_current_locale

logger = logging.getLogger(__name__)

_FORMATTER = string.Formatter()

# Pydantic-Validierungstexte werden über den Fehler-``type`` zugeordnet
# (``app/i18n/validation.py``), nicht über ihren Wortlaut.
_TYPE_MAPPED_PREFIX = "validation."

# Werte einzelner Platzhalter, die selbst übersetzt werden müssen. Alles andere
# (Status-Tokens wie ``success``/``paused``, Namen, IDs, Fremdtexte) wird
# unverändert eingesetzt – das Frontend zeigt dieselben Tokens roh an.
_PARAM_VALUES: dict[str, dict[Locale, dict[str, str]]] = {
    "task": {
        "de": {
            "deployment": "Deployment",
            "deploy": "Deployment",
            "update": "Update",
            "destroy": "Löschen",
            "pause": "Pausieren",
            "resume": "Fortsetzen",
            "redeploy": "Neu-Deployment",
        },
        "en": {},
    },
    "action": {
        "de": {
            "destroy": "Löschen (destroy)",
            "delete": "Entfernen (delete)",
            "pause": "Pausieren (pause)",
            "resume": "Fortsetzen (resume)",
        },
        "en": {},
    },
}


def _localize_param(name: str, value: Any, locale: Locale) -> Any:
    if not isinstance(value, str):
        return value
    if name in ("required", "expected") and locale == "de":
        return value.replace(" or ", " oder ")
    return _PARAM_VALUES.get(name, {}).get(locale, {}).get(value, value)


def t(key: str, locale: Locale | None = None, **params: Any) -> str:
    """Katalog-Text für ``key`` in der Sprache des laufenden Requests.

    Wirft nie: Ein unbekannter Key liefert den Key selbst, ein fehlender oder
    unpassender Platzhalter den unformatierten Text. Beides wird geloggt.
    """
    locale = locale or get_current_locale()
    entry = MESSAGES.get(key)
    if entry is None:
        logger.warning("i18n: unknown message key %r", key)
        return key
    template = entry.get(locale) or entry[DEFAULT_LOCALE]
    params = {name: _localize_param(name, value, locale) for name, value in params.items()}
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        logger.warning("i18n: bad parameters for %r: %r", key, sorted(params))
        return template


def placeholders(template: str) -> list[str]:
    """Namen der Platzhalter in Reihenfolge des Auftretens."""
    return [name for _, name, _, _ in _FORMATTER.parse(template) if name]


@lru_cache(maxsize=1)
def _patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """EN-Vorlagen als Regex, spezifischste (längste Festtext-Anteile) zuerst."""
    compiled: list[tuple[int, str, re.Pattern[str]]] = []
    for key, entry in MESSAGES.items():
        if key.startswith(_TYPE_MAPPED_PREFIX):
            continue
        parts: list[str] = []
        literal_len = 0
        for literal, name, _spec, _conv in _FORMATTER.parse(entry["en"]):
            parts.append(re.escape(literal))
            literal_len += len(literal)
            if name:
                parts.append(f"(?P<{name}>.+?)")
        compiled.append((literal_len, key, re.compile("".join(parts), re.DOTALL)))
    compiled.sort(key=lambda item: -item[0])
    return tuple((key, pattern) for _, key, pattern in compiled)


def find_key(text: str) -> str | None:
    """Katalog-Key, dessen EN-Vorlage ``text`` entspricht (sonst ``None``)."""
    for key, pattern in _patterns():
        if pattern.fullmatch(text):
            return key
    return None


def localize_text(text: str, locale: Locale | None = None) -> str:
    """Übersetzt einen EN-Fehlertext, wenn er einem Katalogeintrag entspricht.

    Unbekannte Texte (z. B. Fremdfehler aus OpenStack) bleiben unverändert.
    EN ist der Quelltext und wird daher nie angefasst.
    """
    locale = locale or get_current_locale()
    if locale == "en":
        return text
    for key, pattern in _patterns():
        match = pattern.fullmatch(text)
        if match:
            return t(key, locale, **match.groupdict())
    return text


def localize_detail(detail: Any, locale: Locale | None = None) -> Any:
    """Wendet ``localize_text`` auf ``HTTPException.detail`` an.

    * ``str`` → übersetzter Text.
    * ``dict`` → Kopie, in der ``message`` übersetzt ist. ``reason``/``code``
      und alle anderen Felder bleiben unangetastet.
    * alles andere (z. B. die Fehlerliste von Pydantic) → unverändert.
    """
    if isinstance(detail, str):
        return localize_text(detail, locale)
    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        return {**detail, "message": localize_text(detail["message"], locale)}
    return detail
