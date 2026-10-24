"""Exception-Handler, der ``HTTPException.detail`` in die Request-Sprache bringt."""

from __future__ import annotations

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.i18n.translate import localize_detail


async def localized_http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    localized = StarletteHTTPException(
        status_code=exc.status_code,
        detail=localize_detail(exc.detail),
        headers=exc.headers,
    )
    return await http_exception_handler(request, localized)
