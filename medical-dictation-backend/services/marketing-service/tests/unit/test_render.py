"""Every template, every language, renders — with nothing sample left in it.

The designed HTML shipped with a worked example inside it (Olena, a
hospital domain, 20 August, a fake Meet room). A single one of those
surviving into production is a mail that tells a real prospect about
somebody else's demo, so the check is exhaustive rather than
representative.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from marketing_service.adapters.templates import KINDS, render_html, template_name
from marketing_service.domain import compose, copy
from marketing_service.domain.language import SUPPORTED

KYIV = ZoneInfo("Europe/Kyiv")
START = datetime(2026, 9, 3, 15, 0, tzinfo=KYIV)
END = datetime(2026, 9, 3, 15, 40, tzinfo=KYIV)
REQUESTED = datetime(2026, 9, 1, 9, 12, tzinfo=UTC)

# Values that must never appear in a rendered mail. Each one is a sample
# from the designed files.
SAMPLE_LEAKS = (
    "Olena",
    "Олено",
    "Frau Weber",
    "orion-med",
    "kln-demo-x1y",
    "kln-req-x1y",
    "T4DkeSNC6bsdCuUy9",
    "20 August 2026",
    "20. August 2026",
    "20 серпня 2026",
)


def ack_fields(lang: str) -> dict[str, str]:
    return compose.request_received_fields(
        lang=lang,
        email="dr.mueller@charite.example",
        first_name=None,
        token="tok-abc",
        requested_at=REQUESTED,
        booking_url="https://calendar.app.google/EXAMPLE",
        public_base_url="https://klarnote.com",
        host_name="Andrii Kovalchuk",
        demo_minutes=40,
    )


def confirm_fields(lang: str) -> dict[str, str]:
    return compose.demo_confirmed_fields(
        lang=lang,
        first_name=None,
        token="tok-abc",
        starts_at=START,
        ends_at=END,
        timezone_name="Europe/Kyiv",
        meet_url="https://meet.google.com/abc-defg-hij",
        booking_url="https://calendar.app.google/EXAMPLE",
        public_base_url="https://klarnote.com",
        host_name="Andrii Kovalchuk",
        reply_to="sales@klarnote.com",
        demo_minutes=40,
    )


def notice_fields(lang: str) -> dict[str, str]:
    """The sales mailbox's copy. `lang` is ignored — see ENGLISH_ONLY below."""
    return compose.contact_internal_fields(
        email="dr.mueller@charite.example",
        first_name="Anna",
        organisation="Charité",
        message="Do you support German dictation?\n\nWe are twelve radiologists.",
        reason="Sales",
        form_lang="de",
        requested_at=REQUESTED,
        source_page="https://klarnote.com/#/contact",
        public_base_url="https://klarnote.com",
    )


# contact_received takes the SAME variables as the demo acknowledgement — one
# compose function feeds both (routers/demo.py picks the template, not the
# fields), so the golden checks below cover it without a second builder. So
# does subscribe_confirmed: its template reads email, requested_at and
# booking_url, all of which the acknowledgement builder already produces.
FIELDS = {
    "request_received": ack_fields,
    "demo_confirmed": confirm_fields,
    "contact_received": ack_fields,
    "subscribe_confirmed": ack_fields,
    "contact_internal": notice_fields,
}

# Not every kind exists in every language, and one deliberately never will.
# contact_internal is the forward to our own sales mailbox: one team, one
# language. Parametrising it over the cross product would demand German and
# Ukrainian translations of a letter nobody wants translated — the visitor's
# words travel inside it untranslated, which is the only part that matters.
ENGLISH_ONLY = frozenset({"contact_internal"})

# Every (kind, language) pair that is supposed to exist. Built as an explicit
# list rather than two stacked parametrize decorators so a kind can opt out of
# a language without every test in the file growing a skip.
CASES = [
    (kind, lang)
    for kind in KINDS
    for lang in (("en",) if kind in ENGLISH_ONLY else SUPPORTED)
]


def test_every_kind_has_a_field_builder() -> None:
    """The tripwire for the gap this file used to have.

    KINDS is extended by a migration-sized change (a new template, a new CHECK
    value); FIELDS is extended here. When the two drift, the parametrised tests
    below fail with a KeyError that reads like a harness bug rather than the
    missing coverage it actually is. This says it plainly instead.
    """
    assert set(FIELDS) == set(KINDS)


@pytest.mark.parametrize("kind,lang", CASES)
def test_every_template_renders(kind: str, lang: str) -> None:
    html = render_html(kind, lang, FIELDS[kind](lang))
    assert html.startswith("<!doctype html>")
    assert "</html>" in html


@pytest.mark.parametrize("kind,lang", CASES)
def test_no_sample_value_survives(kind: str, lang: str) -> None:
    html = render_html(kind, lang, FIELDS[kind](lang))
    for leak in SAMPLE_LEAKS:
        assert leak not in html, f"{template_name(kind, lang)} still contains {leak!r}"


@pytest.mark.parametrize("kind,lang", CASES)
def test_no_unrendered_placeholder_survives(kind: str, lang: str) -> None:
    html = render_html(kind, lang, FIELDS[kind](lang))
    assert "{{" not in html and "}}" not in html


@pytest.mark.parametrize("kind,lang", CASES)
def test_the_font_stacks_reach_the_letter_as_css_not_as_entities(
    kind: str, lang: str
) -> None:
    """`sans` and `cond` are CSS, and autoescaping cannot tell.

    The stacks are quoted family names, so an escaped stack reads
    `font-family:&#39;Geist&#39;, …`. A conformant parser decodes that inside a
    style attribute; several mail clients' inline-CSS parsers do not, and a
    declaration they cannot parse is dropped whole — the letter then falls back
    past every name in the stack to the client default. Marked safe once on the
    definition in _layout.html; this is the guard on all twelve files at once.
    """
    html = render_html(kind, lang, FIELDS[kind](lang))
    assert "&#39;" not in html
    if kind != "contact_internal":  # the one letter that is not on the layout
        assert "font-family:'Geist'" in html
        assert "font-family:'Galgo'" in html


@pytest.mark.parametrize("lang", SUPPORTED)
def test_acknowledgement_carries_the_booking_button(lang: str) -> None:
    """The single thing this mail exists to do."""
    html = render_html("request_received", lang, ack_fields(lang))
    assert 'href="https://calendar.app.google/EXAMPLE"' in html


@pytest.mark.parametrize("lang", SUPPORTED)
def test_confirmation_carries_the_meet_link_and_the_slot(lang: str) -> None:
    fields = confirm_fields(lang)
    html = render_html("demo_confirmed", lang, fields)
    assert 'href="https://meet.google.com/abc-defg-hij"' in html
    assert fields["date_label"] in html
    assert fields["time_label"] in html


@pytest.mark.parametrize("kind,lang", CASES)
def test_text_alternate_renders_with_the_same_fields(kind: str, lang: str) -> None:
    """A missing key must raise here, not print `{meet_url}` to a prospect."""
    text = copy.text_body(kind, lang, FIELDS[kind](lang))
    assert text.strip()
    assert "{" not in text


@pytest.mark.parametrize("kind,lang", CASES)
def test_every_kind_and_language_has_a_subject(kind: str, lang: str) -> None:
    subject = copy.subject_for(kind, lang)
    assert subject and "\n" not in subject


# ── the contact form's forward to the sales mailbox ──────────────────
#
# This letter is the only one addressed to us, and the only one whose body is
# mostly a stranger's free text. Both facts are load-bearing, so both are
# asserted rather than assumed.


def test_the_notice_carries_the_message_verbatim() -> None:
    fields = notice_fields("en")
    text = copy.text_body("contact_internal", "en", fields)
    html = render_html("contact_internal", "en", fields)
    for part in ("Do you support German dictation?", "We are twelve radiologists."):
        assert part in text
        assert part in html
    # And the facts a reader triages on.
    assert "dr.mueller@charite.example" in html
    assert "Charité" in html
    assert "Sales" in html


def test_the_notice_replies_to_the_sender_not_to_us() -> None:
    """The field the delivery worker reads to set Reply-To.

    Without it the worker falls back to the sales mailbox and answering the
    notice mails the team its own copy — the whole point of the letter is that
    replying reaches the person who wrote in.
    """
    assert notice_fields("en")["reply_to"] == "dr.mueller@charite.example"


def test_a_hostile_message_is_escaped_not_executed() -> None:
    """Free text from an unauthenticated form, rendered into HTML mail.

    Asserted on the ANGLE BRACKETS, not on `onerror=`: once `<` is escaped the
    attribute name is inert text, and a substring check for it fails on a
    correctly escaped document. What must never appear is an opened tag.
    """
    hostile = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    fields = notice_fields("en") | {"message": hostile, "first_name": hostile}
    html = render_html("contact_internal", "en", fields)
    assert "<script" not in html
    assert "<img" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img src=x onerror=alert(2)&gt;" in html


def test_a_message_full_of_braces_does_not_break_the_text_alternate() -> None:
    """`str.format` substitutes values, it does not re-scan them.

    A visitor writing `{booking_url}` or a lone `{` into the form must not
    produce a KeyError that dead-letters their enquiry — the template holds the
    field names, and a value is never a template.
    """
    fields = notice_fields("en") | {"message": "Costs {booking_url} and { and }?"}
    text = copy.text_body("contact_internal", "en", fields)
    assert "Costs {booking_url} and { and }?" in text


def test_the_notice_has_no_unsubscribe_link() -> None:
    """It is internal mail to our own inbox; an opt-out on it is nonsense.

    Also a guard on the worker's List-Unsubscribe header, which is populated
    from `unsubscribe_url` in exactly these fields.
    """
    fields = notice_fields("en")
    assert "unsubscribe_url" not in fields
    assert "unsubscribe" not in render_html("contact_internal", "en", fields).lower()


def test_an_anonymous_enquiry_still_renders() -> None:
    """Name, company, reason and page are all optional on the form."""
    fields = compose.contact_internal_fields(
        email="someone@example.test",
        first_name=None,
        organisation=None,
        message="Just the message.",
        reason=None,
        form_lang="en",
        requested_at=REQUESTED,
        source_page=None,
        public_base_url="https://klarnote.com",
    )
    html = render_html("contact_internal", "en", fields)
    assert "Just the message." in html
    assert "someone@example.test" in html
    # Absent facts show no row at all rather than an empty one.
    assert ">Company<" not in html
    assert ">Reason<" not in html


def test_html_is_escaped_not_injected() -> None:
    """The email address arrives from an unauthenticated form."""
    fields = ack_fields("en") | {"email": '<script>alert(1)</script>@x.test'}
    html = render_html("request_received", "en", fields)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_is_deterministic() -> None:
    first = render_html("demo_confirmed", "uk", confirm_fields("uk"))
    second = render_html("demo_confirmed", "uk", confirm_fields("uk"))
    assert first == second


def test_a_missing_variable_fails_loudly() -> None:
    """StrictUndefined: a half-rendered confirmation is worse than none."""
    from jinja2 import UndefinedError

    broken = confirm_fields("en")
    del broken["meet_url"]
    with pytest.raises(UndefinedError):
        render_html("demo_confirmed", "en", broken)


# ── The display type is a raster ─────────────────────────────────────
# Gmail and Outlook strip @font-face, so Galgo never rendered in the clients
# these letters are actually read in. The wordmark and the hero headline ship
# as cid: images instead; everything else stays live text. Added 2026-08-12.

@pytest.mark.parametrize("kind,lang", CASES)
def test_every_layout_letter_has_a_headline_image(kind: str, lang: str) -> None:
    """A missing PNG is a silent downgrade to the fallback stack — catch it."""
    from marketing_service.adapters import mail_images

    if kind == "contact_internal":  # internal notice, not on the layout
        return
    context, images = mail_images.for_letter(kind, lang)
    assert context["headline_cid"] == mail_images.HEADLINE_CID
    assert context["wordmark_cid"] == mail_images.WORDMARK_CID
    assert {image.filename for image in images} == {
        "wordmark.png",
        f"headline.{kind}.{lang}.png",
    }
    # Published at 1×, so a 2× raster must halve. A headline wider than the
    # card's 544px inner width would be scaled down by the client and stop
    # matching the body type.
    assert 0 < context["headline_w"] <= 544


@pytest.mark.parametrize("kind,lang", CASES)
def test_the_image_and_the_text_carry_the_same_words(kind: str, lang: str) -> None:
    """`alt` is the block itself, so the copy cannot drift from the picture.

    It is also what a reader with images off gets, which is why the line
    breaks become spaces rather than disappearing.
    """
    from marketing_service.adapters import mail_images

    if kind == "contact_internal":
        return
    fields = FIELDS[kind](lang)
    context, _ = mail_images.for_letter(kind, lang)
    with_image = render_html(kind, lang, fields | context)
    as_text = render_html(kind, lang, fields)

    assert f'src="cid:{mail_images.HEADLINE_CID}"' in with_image
    assert f'src="cid:{mail_images.WORDMARK_CID}"' in with_image
    # The text fallback is still there when no asset is supplied — a language
    # added before anyone runs the generator must still send.
    assert "cid:" not in as_text
    assert 'class="h1"' in as_text

    # unescape: an apostrophe in "You're on the list." is `&#39;` inside an
    # attribute value and a bare `'` in the template's own body text. Both are
    # right; only the words have to match.
    from html import unescape

    alt = unescape(with_image.split('alt="', 2)[2].split('"')[0])
    headline = as_text.split('class="h1"', 1)[1].split(">", 1)[1].split("</p>")[0]
    assert alt == " ".join(unescape(headline.replace("<br />", " ")).split())


def test_the_alt_filter_keeps_the_words_and_drops_the_markup() -> None:
    from markupsafe import Markup

    from marketing_service.adapters.templates import alt_text

    assert alt_text(Markup("Thank you —<br />your message")) == "Thank you — your message"
    assert alt_text(Markup("It&#39;s booked —<br />see you then.")) == (
        "It's booked — see you then."
    )
