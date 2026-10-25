"""Sprachwahl pro Request.

Das Frontend schickt bei jeder Anfrage ``Accept-Language: de`` bzw. ``en``
(``frontend/src/i18n/locale.ts``, auch beim SSE-Stream). Ein Browser-Header
wie ``de-DE,de;q=0.9,en;q=0.8`` wird ebenfalls verstanden.

Die Sprache liegt in einer ``ContextVar``. Damit kommen auch Stellen an sie
heran, die keinen ``Request`` sehen (CRUD, ``utils/``, Services), ohne dass
deren Signaturen angefasst werden müssen. Starlette kopiert den Kontext in den
Threadpool, in dem die synchronen Endpoints laufen.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Literal

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

Locale = Literal["de", "en"]

SUPPORTED_LOCALES: tuple[Locale, ...] = ("de", "en")

# Gleicher Fallback wie ``DEFAULT_LOCALE`` im Frontend: Fehlt der Header oder
# ist die Sprache unbekannt, kommt Deutsch.
DEFAULT_LOCALE: Locale = "de"

current_locale: ContextVar[Locale] = ContextVar("current_locale", default=DEFAULT_LOCALE)


def get_current_locale() -> Locale:
    """Sprache des laufenden Requests (``DEFAULT_LOCALE`` außerhalb eines Requests)."""
    return current_locale.get()


def normalize_locale(value: str | None) -> Locale | None:
    """``"de-DE"`` / ``"EN"`` → ``"de"`` / ``"en"``; unbekannt → ``None``."""
    if not value:
        return None
    primary = value.strip().lower().replace("_", "-").split("-", 1)[0]
    if primary in SUPPORTED_LOCALES:
        return primary  # type: ignore[return-value]
    return None


def parse_accept_language(header: str | None) -> Locale:
    """Wählt die unterstützte Sprache mit der höchsten Gewichtung.

    Bei gleicher Gewichtung gewinnt die Reihenfolge im Header. Ohne Treffer
    (leer, ``*``, ``fr``, Unsinn) gilt ``DEFAULT_LOCALE``.
    """
    if not header:
        return DEFAULT_LOCALE

    best: Locale | None = None
    best_q = -1.0
    for part in header.split(","):
        tag, _, params = part.partition(";")
        locale = normalize_locale(tag)
        if locale is None:
            continue
        q = 1.0
        params = params.strip()
        if params.lower().startswith("q="):
            try:
                q = float(params[2:])
            except ValueError:
                q = 0.0
        if q <= 0:
            continue
        if q > best_q:
            best, best_q = locale, q
    return best or DEFAULT_LOCALE


class LocaleMiddleware:
    """Setzt ``current_locale`` pro Request und ``Content-Language`` an der Antwort.

    Als reine ASGI-Middleware (kein ``BaseHTTPMiddleware``), damit der
    ``ContextVar`` bis in die Endpoints durchgereicht wird und SSE-Streams
    nicht gepuffert werden.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        locale = parse_accept_language(Headers(scope=scope).get("accept-language"))
        token = current_locale.set(locale)

        async def send_with_language(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Language"] = locale
            await send(message)

        try:
            await self.app(scope, receive, send_with_language)
        finally:
            current_locale.reset(token)
