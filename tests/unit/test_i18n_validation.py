"""422-Fehlerlisten von Pydantic werden anhand des ``type`` übersetzt."""

import uuid
from enum import Enum
from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.i18n import LocaleMiddleware
from app.i18n.validation import _TYPE_KEYS, localized_validation_handler

pytestmark = pytest.mark.unit


class Color(str, Enum):
    RED = "red"
    BLUE = "blue"


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=3, max_length=5, pattern="^[a-z]+$")
    count: int = Field(gt=0, lt=10)
    ratio: float = Field(ge=0.5, le=1.5)
    flag: bool
    ident: uuid.UUID
    tags: list[str] = Field(min_length=1, max_length=2)
    color: Color
    mode: Literal["a", "b"]
    nested: dict[str, int]
    checked: str

    @field_validator("checked")
    @classmethod
    def _checked(cls, value):  # noqa: ARG003
        raise ValueError("App not found")


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(LocaleMiddleware)
    app.add_exception_handler(RequestValidationError, localized_validation_handler)

    @app.post("/payload")
    def payload(body: Payload):
        return {}

    return TestClient(app)


def _post(client, body, locale):
    response = client.post("/payload", json=body, headers={"Accept-Language": locale})
    assert response.status_code == 422
    return {tuple(e["loc"])[-1]: e for e in response.json()["detail"]}


_BAD = {
    "name": "A",
    "count": 0,
    "ratio": 3.0,
    "flag": "maybe",
    "ident": "nope",
    "tags": [],
    "color": "green",
    "mode": "c",
    "nested": [],
    "checked": "x",
    "extra": 1,
}


def test_german_messages_for_each_mapped_error_type(client):
    errors = _post(client, _BAD, "de")
    assert errors["name"]["msg"].startswith("Der Text muss")
    assert errors["count"]["msg"] == "Der Wert muss größer als 0 sein"
    assert errors["ratio"]["msg"] == "Der Wert darf höchstens 1.5 betragen"
    assert errors["flag"]["msg"].startswith("Eingabe muss ein Wahrheitswert")
    assert errors["ident"]["msg"].startswith("Eingabe muss eine gültige UUID")
    assert errors["tags"]["msg"] == "Die Liste muss mindestens 1 Einträge enthalten"
    assert errors["color"]["msg"] == "Eingabe muss 'red' oder 'blue' sein"
    assert errors["mode"]["msg"] == "Eingabe muss 'a' oder 'b' sein"
    assert errors["nested"]["msg"] == "Eingabe muss ein Objekt (Dictionary) sein"
    assert errors["extra"]["msg"] == "Zusätzliche Felder sind nicht erlaubt"


def test_missing_field_and_custom_value_error(client):
    body = {k: v for k, v in _BAD.items() if k != "name"}
    errors = _post(client, body, "de")
    assert errors["name"]["msg"] == "Pflichtfeld fehlt"
    # Eigene Validatoren: Text kommt aus unserem Code und wird über den Katalog übersetzt.
    assert errors["checked"]["msg"] == "Ungültiger Wert: App nicht gefunden"


def test_english_and_structure_are_untouched(client):
    de = _post(client, _BAD, "de")
    en = _post(client, _BAD, "en")
    assert en["name"]["msg"].startswith("String should")
    assert en["checked"]["msg"] == "Value error, App not found"
    for key in de:
        # Nur ``msg`` unterscheidet sich; ``type``, ``loc`` bleiben stabil.
        assert de[key]["type"] == en[key]["type"]
        assert de[key]["loc"] == en[key]["loc"]


def test_unmapped_types_stay_english(client):
    from app.i18n.validation import localize_validation_error

    error = {"type": "datetime_parsing", "loc": ["x"], "msg": "Input should be a valid datetime"}
    assert localize_validation_error(error, "de") == error


def test_every_mapped_type_points_to_a_catalog_entry():
    from app.i18n.catalog import MESSAGES

    assert set(_TYPE_KEYS.values()) <= set(MESSAGES)
