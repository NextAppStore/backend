"""Unit-Tests für ``app.i18n``: Sprachwahl, Katalog, Übersetzung, Handler."""

import ast
import pathlib

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.i18n import (
    LocaleMiddleware,
    get_current_locale,
    localize_detail,
    localize_text,
    parse_accept_language,
    t,
)
from app.i18n.catalog import MESSAGES
from app.i18n.handlers import localized_http_exception_handler
from app.i18n.translate import find_key, placeholders

pytestmark = pytest.mark.unit

APP_DIR = pathlib.Path(__file__).resolve().parents[2] / "app"


# ---------------------------------------------------------------------------
# Accept-Language
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "header, expected",
    [
        ("de", "de"),
        ("en", "en"),
        ("EN", "en"),
        ("de-DE,de;q=0.9,en;q=0.8", "de"),
        ("en-US,en;q=0.9,de;q=0.8", "en"),
        ("fr,en;q=0.5", "en"),
        ("fr, de;q=0.2, en;q=0.9", "en"),
        ("en;q=0, de", "de"),
        ("fr", "de"),
        ("*", "de"),
        ("", "de"),
        (None, "de"),
        ("???;;q=abc", "de"),
        ("en;q=abc", "de"),
    ],
)
def test_parse_accept_language(header, expected):
    assert parse_accept_language(header) == expected


# ---------------------------------------------------------------------------
# Katalog
# ---------------------------------------------------------------------------
def test_catalog_has_both_languages_with_same_placeholders():
    for key, entry in MESSAGES.items():
        assert entry["en"].strip(), key
        assert entry["de"].strip(), key
        assert sorted(placeholders(entry["en"])) == sorted(placeholders(entry["de"])), key


def test_catalog_en_templates_are_unambiguous():
    """Zwei Einträge mit gleichem EN-Text würden sich gegenseitig überdecken."""
    seen: dict[str, str] = {}
    for key, entry in MESSAGES.items():
        if key.startswith("validation."):
            continue  # per Pydantic-``type`` zugeordnet, nicht per Wortlaut
        assert entry["en"] not in seen, f"{key} dupliziert {seen[entry['en']]}"
        seen[entry["en"]] = key


def test_t_formats_parameters():
    assert t("approval.reject_status", "en", status="approved") == (
        "Cannot reject a version with status 'approved'"
    )
    assert t("approval.reject_status", "de", status="approved") == (
        "Eine Version im Status 'approved' kann nicht abgelehnt werden"
    )


def test_t_never_raises():
    assert t("does.not.exist", "de") == "does.not.exist"
    # Fehlender Parameter: unformatierter Text statt KeyError.
    assert "{status}" in t("approval.reject_status", "en")


def test_t_uses_request_locale_by_default():
    assert get_current_locale() == "de"
    assert t("app.not_found") == "App nicht gefunden"


def test_t_translates_selected_parameter_values():
    assert "Pausieren" in t("deployment.dispatch_failed", "de", task="pause")
    assert t("deployment.action_status", "de", action="pause", status="x", required="a or b").endswith(
        "Erforderlicher Status: a oder b."
    )
    # EN bleibt wörtlich.
    assert t("deployment.dispatch_failed", "en", task="pause") == (
        "Could not dispatch pause task — please retry"
    )


# ---------------------------------------------------------------------------
# Übersetzung vorhandener Fehlertexte
# ---------------------------------------------------------------------------
def test_localize_text_translates_known_message():
    assert localize_text("Deployment not found", "de") == "Deployment nicht gefunden"
    assert localize_text("Deployment not found", "en") == "Deployment not found"


def test_localize_text_extracts_parameters():
    assert localize_text("Cannot revoke a version with status 'pending'", "de") == (
        "Die Freigabe einer Version im Status 'pending' kann nicht widerrufen werden"
    )
    assert localize_text(
        "Cannot pause a deployment in status 'failed'. Required status: "
        "success, pause_failed or resume_failed.",
        "de",
    ) == (
        "Aktion 'Pausieren (pause)' ist für ein Deployment im Status 'failed' nicht möglich. "
        "Erforderlicher Status: success, pause_failed oder resume_failed."
    )


def test_localize_text_prefers_the_more_specific_template():
    assert localize_text("Invalid LTI session token: missing typ claim", "de") == (
        "Ungültiges LTI-Session-Token: typ-Claim fehlt"
    )
    assert localize_text("Invalid LTI session token: Signature expired", "de") == (
        "Ungültiges LTI-Session-Token: Signature expired"
    )


def test_localize_text_leaves_unknown_text_alone():
    assert localize_text("Quota exceeded for ram", "de") == "Quota exceeded for ram"


def test_localize_detail_only_touches_message():
    detail = {"reason": "openstack_unavailable", "message": "App not found", "n": 3}
    assert localize_detail(detail, "de") == {
        "reason": "openstack_unavailable",
        "message": "App nicht gefunden",
        "n": 3,
    }
    # Eingabe bleibt unverändert.
    assert detail["message"] == "App not found"
    assert localize_detail({"code": "role_required"}, "de") == {"code": "role_required"}
    errors = [{"loc": ["body"], "msg": "field required"}]
    assert localize_detail(errors, "de") is errors


# ---------------------------------------------------------------------------
# Vollständigkeit: jeder Fehlertext im Code hat einen Katalogeintrag
# ---------------------------------------------------------------------------
def _literal_template(node: ast.AST) -> str | None:
    """Textvorlage eines Literals; ``{expr}`` wird zu einem Beispielwert."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "x" for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_template(node.left), _literal_template(node.right)
        return None if left is None or right is None else left + right
    return None


def _user_facing_texts() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if "i18n" in path.parts:
            continue
        rel = str(path.relative_to(APP_DIR.parent))
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            candidates: list[ast.AST] = []
            if isinstance(node, ast.keyword) and node.arg == "detail":
                value = node.value
                if isinstance(value, ast.Dict):
                    candidates += [
                        v
                        for k, v in zip(value.keys, value.values, strict=True)
                        if isinstance(k, ast.Constant) and k.value == "message"
                    ]
                else:
                    candidates.append(value)
            elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in (
                "_bad",
                "PackerTemplateDiscoveryError",
            ):
                candidates += node.args
            elif (
                path.name == "openstack_validator.py"
                and isinstance(node, ast.Return)
                and isinstance(node.value, ast.Tuple)
                and len(node.value.elts) == 2
                and isinstance(node.value.elts[0], ast.Constant)
                and node.value.elts[0].value is False
            ):
                candidates.append(node.value.elts[1])
            elif path.name == "git_service.py" and isinstance(node, ast.Dict):
                candidates += [
                    v
                    for k, v in zip(node.keys, node.values, strict=True)
                    if isinstance(k, ast.Constant) and k.value == "message"
                ]
            for cand in candidates:
                text = _literal_template(cand)
                if text is not None:
                    found.append((rel, cand.lineno, text))
    return found


def test_every_user_facing_error_text_has_a_catalog_entry():
    texts = _user_facing_texts()
    assert len(texts) > 100, "AST-Scan findet auffällig wenige Texte"
    missing = [f"{f}:{line}  {text!r}" for f, line, text in texts if find_key(text) is None]
    assert not missing, "Katalogeintrag fehlt für:\n  " + "\n  ".join(missing)


# ---------------------------------------------------------------------------
# Middleware + Handler
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(LocaleMiddleware)
    app.add_exception_handler(StarletteHTTPException, localized_http_exception_handler)

    @app.get("/plain")
    def plain():
        raise HTTPException(status_code=404, detail="Deployment not found")

    @app.get("/structured")
    def structured():
        raise HTTPException(
            status_code=422,
            detail={"reason": "invalid_base64", "message": "image payload is not valid base64"},
        )

    @app.get("/foreign")
    def foreign():
        raise HTTPException(status_code=502, detail="Some upstream failure")

    @app.get("/headers")
    def with_headers():
        raise HTTPException(
            status_code=401, detail="Invalid token", headers={"WWW-Authenticate": "Bearer"}
        )

    @app.get("/locale")
    def locale():
        return {"locale": get_current_locale()}

    return TestClient(app)


@pytest.mark.parametrize(
    "header, expected",
    [
        ({"Accept-Language": "de"}, "Deployment nicht gefunden"),
        ({"Accept-Language": "en"}, "Deployment not found"),
        ({"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}, "Deployment nicht gefunden"),
        ({"Accept-Language": "fr"}, "Deployment nicht gefunden"),
        ({}, "Deployment nicht gefunden"),
    ],
)
def test_handler_translates_detail(client, header, expected):
    response = client.get("/plain", headers=header)
    assert response.status_code == 404
    assert response.json() == {"detail": expected}


def test_handler_sets_content_language(client):
    assert client.get("/plain", headers={"Accept-Language": "en"}).headers["content-language"] == "en"
    assert client.get("/plain", headers={"Accept-Language": "de"}).headers["content-language"] == "de"
    assert client.get("/plain").headers["content-language"] == "de"


def test_handler_translates_structured_message_and_keeps_reason(client):
    body = client.get("/structured", headers={"Accept-Language": "de"}).json()
    assert body == {
        "detail": {
            "reason": "invalid_base64",
            "message": "Die Bilddaten sind kein gültiges Base64",
        }
    }
    body = client.get("/structured", headers={"Accept-Language": "en"}).json()
    assert body["detail"]["message"] == "image payload is not valid base64"


def test_handler_leaves_foreign_text_and_keeps_headers(client):
    assert client.get("/foreign", headers={"Accept-Language": "de"}).json() == {
        "detail": "Some upstream failure"
    }
    response = client.get("/headers", headers={"Accept-Language": "de"})
    assert response.json() == {"detail": "Ungültiges Token"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_handler_translates_unknown_route(client):
    assert client.get("/nope", headers={"Accept-Language": "de"}).json() == {
        "detail": "Nicht gefunden"
    }


def test_locale_is_visible_inside_the_endpoint_and_does_not_leak(client):
    assert client.get("/locale", headers={"Accept-Language": "en"}).json() == {"locale": "en"}
    assert client.get("/locale").json() == {"locale": "de"}
    assert get_current_locale() == "de"
