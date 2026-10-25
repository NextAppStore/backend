"""E-Mail-Templates: je Sprache eine Variante, Sprache kommt aus ``locale``."""

import pathlib
from types import SimpleNamespace

import pytest

from app.i18n import t
from app.services import email_service

pytestmark = pytest.mark.unit

TEMPLATES = pathlib.Path(email_service._TEMPLATES_DIR)

_ACCESS = {
    "username": "alice",
    "password": "s3cret",
    "auth_type": "password",
    "auth_value": None,
    "ip": "1.2.3.4",
    "port": 8080,
    "url": "http://1.2.3.4:8080",
}
_USER = SimpleNamespace(username="alice", email="alice@example.com")
_DEPLOYMENT = {"name": "demo", "app_name": "app", "git_url": "g", "release_tag": "v1"}


def _user_ctx():
    return {
        "user_display_name": "Alice",
        "deployment": _DEPLOYMENT,
        "user": _USER,
        "access": _ACCESS,
        "teammates": [{"user": _USER, "display_name": "Bob"}],
        "team_name": "Team-1",
    }


def _owner_ctx():
    member = {"user": _USER, "display_name": "Alice", "access": _ACCESS}
    return {
        "owner_display_name": "Olga",
        "deployment": _DEPLOYMENT,
        "detail_url": "http://x/deployments/1",
        "teams": [{"name": "Team-1", "vm": {"url": "http://vm"}, "members": [member]}],
    }


@pytest.mark.parametrize("suffix", ["html", "txt"])
def test_user_invite_is_rendered_in_the_requested_language(suffix):
    de = email_service.render(f"user_invite.{suffix}", "de", **_user_ctx())
    en = email_service.render(f"user_invite.{suffix}", "en", **_user_ctx())
    assert "Hallo" in de and "Passwort" in de and ("Dein Team" in de or "TEAM" in de)
    assert "Hi" in en and "Password" in en
    assert "Hallo" not in en
    assert "Hi " not in de
    # Zugangsdaten stehen in beiden Sprachen drin.
    for body in (de, en):
        assert "s3cret" in body
        assert "1.2.3.4:8080" in body


@pytest.mark.parametrize("suffix", ["html", "txt"])
def test_owner_summary_is_rendered_in_the_requested_language(suffix):
    de = email_service.render(f"owner_summary.{suffix}", "de", **_owner_ctx())
    en = email_service.render(f"owner_summary.{suffix}", "en", **_owner_ctx())
    assert "Hallo" in de and "Mitglieder" in de
    assert "Hi" in en and "Members" in en
    assert "Mitglieder" not in en


def test_html_mail_footer_and_lang_follow_the_locale():
    de = email_service.render("user_invite.html", "de", **_user_ctx())
    en = email_service.render("user_invite.html", "en", **_user_ctx())
    assert '<html lang="de">' in de and "Bitte nicht antworten" in de
    assert '<html lang="en">' in en and "Do not reply" in en


def test_render_defaults_to_the_request_language():
    assert "Hallo" in email_service.render("user_invite.txt", **_user_ctx())


def test_every_mail_template_exists_in_both_languages():
    names = {p.name for p in TEMPLATES.glob("*.*.*")}
    for base in ("user_invite", "owner_summary"):
        for ext in ("html", "txt"):
            assert f"{base}.de.{ext}" in names
            assert f"{base}.en.{ext}" in names


def test_german_and_english_templates_use_the_same_variables():
    """Eine Variable, die nur in einer Sprache gerendert wird, fehlt in der anderen."""
    import re

    pattern = re.compile(r"{{\s*([\w.]+)")
    for base in ("user_invite", "owner_summary"):
        for ext in ("html", "txt"):
            de = (TEMPLATES / f"{base}.de.{ext}").read_text()
            en = (TEMPLATES / f"{base}.en.{ext}").read_text()
            assert sorted(set(pattern.findall(de))) == sorted(set(pattern.findall(en))), (base, ext)


def test_subjects():
    assert t("mail.user_subject", "de") == "Deine Zugangsdaten"
    assert t("mail.user_subject", "en") == "Your access details"
    assert t("mail.owner_subject", "en") == "Deployment summary"
