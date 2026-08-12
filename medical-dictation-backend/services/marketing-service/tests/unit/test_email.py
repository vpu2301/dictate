"""The MIME document and the SMTP greeting."""

from __future__ import annotations

import pytest

from marketing_service.adapters.email import (
    MockProvider,
    OutboundEmail,
    build_mime,
    build_provider,
    ehlo_hostname,
)


def message(**kw: object) -> OutboundEmail:
    base = {
        "to_address": "dr@klinik.example.org",
        "subject": "Your Klarnote demo is booked",
        "text_body": "plain",
        "html_body": "<p>rich</p>",
    }
    base.update(kw)
    return OutboundEmail(**base)  # type: ignore[arg-type]


def test_ehlo_hostname_is_the_sending_domain_never_a_dns_lookup() -> None:
    """`socket.getfqdn()` blocks for 30s behind a router with no PTR."""
    assert ehlo_hostname("sales@klarnote.com") == "klarnote.com"
    assert ehlo_hostname("") == "localhost"
    assert ehlo_hostname("malformed") == "malformed"


def test_mime_is_multipart_alternative_with_both_parts() -> None:
    mime = build_mime(message(), from_address="sales@klarnote.com", from_name="Klarnote")
    types = {part.get_content_type() for part in mime.walk()}
    assert "text/plain" in types
    assert "text/html" in types


def test_mime_carries_a_message_id_and_a_date() -> None:
    """Both absent is a strong spam signal at every major provider."""
    mime = build_mime(message(), from_address="sales@klarnote.com", from_name="Klarnote")
    assert mime["Message-ID"].endswith("klarnote.com>")
    assert mime["Date"]


def test_reply_to_is_set_when_given_and_absent_when_not() -> None:
    with_reply = build_mime(
        message(reply_to="sales@klarnote.com"),
        from_address="sales@klarnote.com",
        from_name="",
    )
    without = build_mime(message(), from_address="sales@klarnote.com", from_name="")
    assert with_reply["Reply-To"] == "sales@klarnote.com"
    assert without["Reply-To"] is None


def test_one_click_headers_travel_together_when_claimed() -> None:
    mime = build_mime(
        message(
            list_unsubscribe="https://klarnote.com/unsubscribe?t=x",
            list_unsubscribe_one_click=True,
        ),
        from_address="sales@klarnote.com",
        from_name="",
    )
    assert mime["List-Unsubscribe"] == "<https://klarnote.com/unsubscribe?t=x>"
    assert mime["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_the_value_can_ship_without_the_one_click_claim() -> None:
    """A mailto: unsubscribe is valid; RFC 8058 one-click on it is not."""
    mime = build_mime(
        message(list_unsubscribe="mailto:sales@klarnote.com?subject=unsubscribe"),
        from_address="sales@klarnote.com",
        from_name="",
    )
    assert mime["List-Unsubscribe"] == "<mailto:sales@klarnote.com?subject=unsubscribe>"
    assert mime["List-Unsubscribe-Post"] is None


def test_not_marked_auto_submitted() -> None:
    """These invite a reply — suppressing the recipient's is the wrong default."""
    mime = build_mime(message(), from_address="sales@klarnote.com", from_name="")
    assert mime["Auto-Submitted"] is None


def test_ics_rides_along_as_a_calendar_attachment() -> None:
    mime = build_mime(
        message(attachments=(("klarnote-demo.ics", "calendar", "BEGIN:VCALENDAR\r\n"),)),
        from_address="sales@klarnote.com",
        from_name="",
    )
    attached = [p for p in mime.walk() if p.get_filename() == "klarnote-demo.ics"]
    assert len(attached) == 1
    assert attached[0].get_content_type() == "text/calendar"


def test_mock_provider_refuses_production() -> None:
    """A mock that accepts mail in production looks exactly like success."""
    with pytest.raises(RuntimeError, match="never be used in production"):
        MockProvider(is_production=True)
    with pytest.raises(RuntimeError):
        build_provider(kind="mock", is_production=True)


def test_unknown_provider_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown email provider"):
        build_provider(kind="sendgrid", is_production=False)


async def test_mock_provider_records_what_it_was_given() -> None:
    provider = MockProvider()
    result = await provider.send(message())
    assert provider.sent[0].subject == "Your Klarnote demo is booked"
    assert result.provider_message_id


# ── One-click unsubscribe is a promise, not a formatting rule ────────
# Two silent failures, one header. A dev MDX_PUBLIC_BASE_URL in a run wired to a
# real relay puts an http://localhost URL on it, which RFC 8058 forbids outright.
# The subtler one is an https:// URL that nothing serves — well-formed, believed,
# and the receiver's POST to it fails. Either way the submission relay answers
# 250, the far end drops or junks the mail, and every outbox row reads `sent`
# while nobody receives anything. Diagnosed 2026-08-12: klarnote.com had no
# /unsubscribe route and no TLS certificate at all.

MAILTO = "mailto:sales@klarnote.com?subject=unsubscribe"


def test_a_non_https_unsubscribe_url_falls_back_to_mailto() -> None:
    from marketing_service.delivery.worker import unsubscribe_header

    for url in ("http://localhost:5173/unsubscribe?t=abc", "http://klarnote.com/u?t=a"):
        assert unsubscribe_header(
            url, one_click=True, mailto="sales@klarnote.com"
        ) == (MAILTO, False)


def test_an_unproven_https_url_falls_back_to_mailto() -> None:
    """The URL is well-formed. Nothing serves it — so it may not be claimed."""
    from marketing_service.delivery.worker import unsubscribe_header

    assert unsubscribe_header(
        "https://klarnote.com/unsubscribe?t=abc",
        one_click=False,
        mailto="sales@klarnote.com",
    ) == (MAILTO, False)


def test_an_https_url_is_claimed_once_the_endpoint_is_declared_live() -> None:
    from marketing_service.delivery.worker import unsubscribe_header

    url = "https://klarnote.com/unsubscribe?t=abc"
    assert unsubscribe_header(url, one_click=True, mailto="sales@klarnote.com") == (
        url,
        True,
    )


def test_with_no_url_and_no_mailto_there_is_no_header_at_all() -> None:
    """"" is what build_mime already treats as absent — assert it, don't assume."""
    from marketing_service.adapters.email import OutboundEmail, build_mime
    from marketing_service.delivery.worker import unsubscribe_header

    assert unsubscribe_header("  ", one_click=True, mailto="") == ("", False)
    mime = build_mime(
        OutboundEmail(
            to_address="a@example.com", subject="s", text_body="t", list_unsubscribe=""
        ),
        from_address="sales@klarnote.com",
        from_name="Klarnote",
        message_id="<x@klarnote.com>",
        date="Wed, 12 Aug 2026 09:00:00 +0000",
    )
    assert mime["List-Unsubscribe"] is None
    assert mime["List-Unsubscribe-Post"] is None
