"""Rendering and MIME assembly for the two account-security mails.

The parametrised render test is the important one: it proves every
kind × language combination produces a mail with no unsubstituted
variables and no leftover sample text, which is the failure that only
ever shows up in somebody's inbox.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from auth_service.adapters import email as email_mod
from auth_service.adapters import templates
from auth_service.domain import compose
from auth_service.domain import copy as copy_mod

APP = "https://app.klarnote.com"
SUPPORT = "https://klarnote.com/contact"
WHEN = datetime(2026, 8, 9, 14, 30, tzinfo=UTC)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120.0 Safari/537.36"
TOKEN = "tok-ABCDEF0123456789"


def _fields(kind: str, lang: str) -> tuple[dict, dict]:
    if kind == copy_mod.KIND_PASSWORD_RESET:
        return (
            compose.password_reset_fields(
                lang=lang,
                email="olena@clinic.example",
                display_name="Olena",
                user_agent=UA,
                support_url=SUPPORT,
                app_base_url=APP,
                requested_at=WHEN,
                ttl_seconds=1800,
            ),
            compose.reset_secret_fields(app_base_url=APP, token=TOKEN),
        )
    return (
        compose.password_changed_fields(
            lang=lang,
            email="olena@clinic.example",
            display_name="Olena",
            user_agent=UA,
            support_url=SUPPORT,
            app_base_url=APP,
            changed_at=WHEN,
            lockdown_ttl_seconds=604800,
        ),
        compose.lockdown_secret_fields(app_base_url=APP, token=TOKEN),
    )


def _render(kind: str, lang: str) -> templates.RenderedEmail:
    fields, secrets = _fields(kind, lang)
    return templates.render(
        kind,
        lang,
        subject=copy_mod.subject_for(kind, lang),
        text_body=copy_mod.text_body(
            kind, lang, compose.text_values(kind, lang, fields, secrets)
        ),
        context={**fields, **secrets},
    )


@pytest.mark.parametrize("kind", copy_mod.KINDS)
@pytest.mark.parametrize("lang", copy_mod.SUPPORTED_LANGS)
def test_every_kind_and_language_renders(kind: str, lang: str) -> None:
    rendered = _render(kind, lang)
    assert rendered.subject
    assert "\n" not in rendered.subject  # header-injection guard
    assert rendered.html_body.startswith("<!doctype html>")
    assert rendered.text_body
    # No unsubstituted Jinja or str.format placeholders survived.
    assert "{{" not in rendered.html_body
    assert "}}" not in rendered.html_body
    assert "{" not in rendered.text_body


@pytest.mark.parametrize("kind", copy_mod.KINDS)
@pytest.mark.parametrize("lang", copy_mod.SUPPORTED_LANGS)
def test_action_link_is_present_in_both_parts(kind: str, lang: str) -> None:
    """The one thing the mail exists to deliver must be in both parts."""
    rendered = _render(kind, lang)
    assert TOKEN in rendered.html_body
    assert TOKEN in rendered.text_body


@pytest.mark.parametrize("lang", copy_mod.SUPPORTED_LANGS)
def test_security_mail_carries_the_lockdown_route(lang: str) -> None:
    rendered = _render(copy_mod.KIND_PASSWORD_CHANGED, lang)
    assert "/#/account-recovery?token=" in rendered.html_body


@pytest.mark.parametrize("lang", copy_mod.SUPPORTED_LANGS)
def test_reset_mail_carries_the_reset_route(lang: str) -> None:
    rendered = _render(copy_mod.KIND_PASSWORD_RESET, lang)
    assert "/#/reset-password?token=" in rendered.html_body


def test_missing_variable_raises_rather_than_rendering_a_blank_link() -> None:
    """StrictUndefined is load-bearing, not decoration."""
    from jinja2 import UndefinedError

    with pytest.raises(UndefinedError):
        templates.render_html(
            copy_mod.KIND_PASSWORD_RESET, "en", {"greeting": "Hello,"}
        )


def test_unknown_kind_and_language_are_refused() -> None:
    with pytest.raises(ValueError):
        templates.template_name("not_a_kind", "en")
    with pytest.raises(ValueError):
        templates.template_name(copy_mod.KIND_PASSWORD_RESET, "fr")


def test_attacker_supplied_email_is_html_escaped() -> None:
    """The address is attacker-chosen on the forgot endpoint."""
    fields, secrets = _fields(copy_mod.KIND_PASSWORD_RESET, "en")
    fields["email"] = "<script>alert(1)</script>@x.com"
    html = templates.render_html(
        copy_mod.KIND_PASSWORD_RESET, "en", {**fields, **secrets}
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_subject_newlines_are_stripped() -> None:
    rendered = templates.render(
        copy_mod.KIND_PASSWORD_RESET,
        "en",
        subject="Reset\r\nBcc: attacker@evil.example",
        text_body="body",
        context=dict(
            **_fields(copy_mod.KIND_PASSWORD_RESET, "en")[0],
            **_fields(copy_mod.KIND_PASSWORD_RESET, "en")[1],
        ),
    )
    assert "\n" not in rendered.subject
    assert "\r" not in rendered.subject


# ── Language and formatting ──────────────────────────────────────────


def test_language_falls_back_to_english() -> None:
    assert copy_mod.normalise_lang("fr") == "en"
    assert copy_mod.normalise_lang(None) == "en"
    assert copy_mod.normalise_lang("de-AT") == "de"
    assert copy_mod.normalise_lang("UK") == "uk"


def test_ukrainian_minutes_pluralisation_handles_the_teens() -> None:
    """11 takes the plural, 1 and 21 take the singular. The teens are
    the case a naive `n == 1` check gets wrong."""
    assert copy_mod.minutes_label(60, "uk") == "1 хвилина"
    assert copy_mod.minutes_label(11 * 60, "uk") == "11 хвилин"
    assert copy_mod.minutes_label(21 * 60, "uk") == "21 хвилина"
    assert copy_mod.minutes_label(3 * 60, "uk") == "3 хвилини"


def test_greeting_without_a_name_is_still_grammatical() -> None:
    for lang in copy_mod.SUPPORTED_LANGS:
        assert copy_mod.greeting(lang, "").strip()
        assert "{" not in copy_mod.greeting(lang, "")


def test_client_label_is_coarse_not_a_fingerprint() -> None:
    assert compose.client_label(UA, "en") == "Chrome on macOS"
    # Nothing usable → a named fallback, never an empty row.
    assert compose.client_label("", "en") == "Unrecognised device"
    assert compose.client_label("", "de") == "Unbekanntes Gerät"


def test_client_line_collapses_when_the_device_is_unknown() -> None:
    """An 'Unrecognised device' line in the text part reads as missing
    data; better to omit the line entirely."""
    fields, secrets = _fields(copy_mod.KIND_PASSWORD_RESET, "en")
    fields["client_label"] = "Unrecognised device"
    values = compose.text_values(
        copy_mod.KIND_PASSWORD_RESET, "en", fields, secrets
    )
    assert values["client_line"] == ""


def test_ip_hash_is_salted_and_truncated() -> None:
    a = compose.hash_ip("203.0.113.7", salt="salt-a")
    b = compose.hash_ip("203.0.113.7", salt="salt-b")
    assert a != b, "an unsalted IP hash is reversible by brute force"
    assert len(a) == 32
    assert compose.hash_ip("", salt="s") == ""


# ── MIME assembly ────────────────────────────────────────────────────


def test_mime_has_both_parts_and_the_headers_deliverability_needs() -> None:
    mime = email_mod.build_mime(
        email_mod.OutboundEmail(
            to_address="olena@clinic.example",
            subject="Reset your Klarnote password",
            text_body="text",
            html_body="<p>html</p>",
            reply_to="sales@klarnote.com",
        ),
        from_address="sales@klarnote.com",
        from_name="Klarnote",
    )
    assert mime.get_content_type() == "multipart/alternative"
    assert mime["Message-ID"]
    assert mime["Date"]
    assert mime["Reply-To"] == "sales@klarnote.com"
    # Security mail IS auto-generated: suppress out-of-office replies.
    assert mime["Auto-Submitted"] == "auto-generated"


def test_security_mail_is_not_unsubscribable() -> None:
    """RFC 8058 is for bulk mail. An unsubscribe path on a security
    notification would let whoever already holds the mailbox silence the
    one warning that would expose them."""
    mime = email_mod.build_mime(
        email_mod.OutboundEmail(
            to_address="a@b.example", subject="s", text_body="t", html_body="<p>h</p>"
        ),
        from_address="sales@klarnote.com",
        from_name="Klarnote",
    )
    assert mime["List-Unsubscribe"] is None
    assert mime["List-Unsubscribe-Post"] is None


def test_ehlo_hostname_never_resolves_the_local_fqdn() -> None:
    """socket.getfqdn() blocks for 30s on a network with no PTR record,
    inside the send, inside the transaction holding the outbox row."""
    assert email_mod.ehlo_hostname("sales@klarnote.com") == "klarnote.com"
    assert email_mod.ehlo_hostname("") == "localhost"


def test_mock_provider_refuses_to_run_in_production() -> None:
    with pytest.raises(RuntimeError, match="never be used in production"):
        email_mod.MockProvider(is_production=True)
    with pytest.raises(RuntimeError):
        email_mod.build_provider(kind="mock", is_production=True)


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown email provider"):
        email_mod.build_provider(kind="carrier-pigeon", is_production=False)


async def test_mock_provider_captures_mail() -> None:
    provider = email_mod.MockProvider()
    result = await provider.send(
        email_mod.OutboundEmail(
            to_address="a@b.example", subject="s", text_body="t"
        )
    )
    assert provider.sent[0].to_address == "a@b.example"
    assert result.provider_message_id
