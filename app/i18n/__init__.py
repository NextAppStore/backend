"""Zweisprachige (DE/EN) Texte für Antworten, Mails und Task-Logs.

Siehe ``catalog.py`` für die Konventionen.
"""

from app.i18n.locale import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    Locale,
    LocaleMiddleware,
    current_locale,
    get_current_locale,
    parse_accept_language,
)
from app.i18n.translate import localize_detail, localize_text, t

__all__ = [
    "DEFAULT_LOCALE",
    "SUPPORTED_LOCALES",
    "Locale",
    "LocaleMiddleware",
    "current_locale",
    "get_current_locale",
    "localize_detail",
    "localize_text",
    "parse_accept_language",
    "t",
]
