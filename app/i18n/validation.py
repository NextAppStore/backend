"""Übersetzung der 422-Fehlerliste, die FastAPI/Pydantic selbst erzeugt.

Anders als ``HTTPException`` kommen diese Texte nicht aus unserem Code. Sie
werden daher nicht über den Wortlaut, sondern über den stabilen Pydantic-
``type`` (``missing``, ``string_too_short`` …) zugeordnet. Unbekannte Typen
(Datumsformate, URL-Parser …) bleiben unverändert englisch.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.i18n.locale import Locale, get_current_locale
from app.i18n.translate import localize_text, t

# Pydantic-``type`` → Katalog-Key. Die Platzhalter heißen wie die Einträge in
# ``ctx`` des jeweiligen Fehlers (z. B. ``min_length``, ``gt``).
_TYPE_KEYS: dict[str, str] = {
    "missing": "validation.missing",
    "string_type": "validation.string_type",
    "string_too_short": "validation.string_too_short",
    "string_too_long": "validation.string_too_long",
    "string_pattern_mismatch": "validation.string_pattern_mismatch",
    "int_type": "validation.int_type",
    "int_parsing": "validation.int_parsing",
    "float_type": "validation.float_type",
    "float_parsing": "validation.float_parsing",
    "bool_type": "validation.bool_type",
    "bool_parsing": "validation.bool_parsing",
    "uuid_type": "validation.uuid_type",
    "uuid_parsing": "validation.uuid_parsing",
    "list_type": "validation.list_type",
    "dict_type": "validation.dict_type",
    "model_type": "validation.model_type",
    "model_attributes_type": "validation.model_type",
    "json_invalid": "validation.json_invalid",
    "extra_forbidden": "validation.extra_forbidden",
    "enum": "validation.enum",
    "literal_error": "validation.literal_error",
    "greater_than": "validation.greater_than",
    "greater_than_equal": "validation.greater_than_equal",
    "less_than": "validation.less_than",
    "less_than_equal": "validation.less_than_equal",
    "too_short": "validation.too_short",
    "too_long": "validation.too_long",
}

_VALUE_ERROR_PREFIX = "Value error, "


def localize_validation_error(error: dict[str, Any], locale: Locale) -> dict[str, Any]:
    """Kopie eines Pydantic-Fehlers mit übersetztem ``msg``."""
    if locale == "en":
        return error
    msg = error.get("msg", "")
    error_type = error.get("type", "")

    if error_type == "value_error" and msg.startswith(_VALUE_ERROR_PREFIX):
        # Eigene Validatoren: der Text hinter dem Präfix kommt aus unserem Code.
        inner = localize_text(msg[len(_VALUE_ERROR_PREFIX) :], locale)
        return {**error, "msg": t("validation.value_error", locale, error=inner)}

    key = _TYPE_KEYS.get(error_type)
    if key is None:
        return error
    return {**error, "msg": t(key, locale, **(error.get("ctx") or {}))}


async def localized_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    locale = get_current_locale()
    errors = [localize_validation_error(e, locale) for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})
