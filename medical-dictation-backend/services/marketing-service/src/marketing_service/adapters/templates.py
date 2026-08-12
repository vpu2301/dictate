"""Jinja2 rendering of the outbound marketing emails.

The HTML files in ``templates/`` are the designed documents, unchanged
except that their sample values became variables. Keeping them whole —
rather than assembling them from translated fragments — is what makes
the German and Ukrainian copy proofreadable by someone who does not read
Python.

Three properties, in priority order:

1. **Autoescaped.** Every variable here is attacker-influenced: the
   email address came from an unauthenticated form, and the calendar
   summary came from an inbound message. Unescaped, either is an
   HTML-injection vector in a mail client.
2. **Strict.** A typo'd variable must fail the render, not quietly
   produce "Your demo is booked for ." — a half-rendered confirmation
   is worse than a bounced one, because nobody finds out.
3. **Deterministic.** Nothing here reads a clock or a random source, so
   the same inputs render byte-identical output and the golden test can
   assert on exact bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

TEMPLATE_DIR = Path(__file__).parent / "templates"

KINDS = (
    "request_received",
    "demo_confirmed",
    "contact_received",
    "subscribe_confirmed",
    # Internal, English-only, and deliberately NOT built on _layout.html: the
    # shared frame is a marketing letter with a booking CTA and an unsubscribe
    # footer, and neither belongs on a work notification addressed to us.
    "contact_internal",
)


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    text_body: str
    html_body: str


def alt_text(value: Any) -> str:
    """A headline block's markup reduced to the words, for an `alt` attribute.

    The headlines carry `<br />` between their lines. `|striptags` alone would
    weld "Thank you —" to "your message", and `|replace('<br />', ' ')` cannot
    match at all: the block's output is Markup, so replace() escapes its own
    argument into `&lt;br /&gt;` before looking for it. Hence a filter.

    Returns a plain str, not Markup — the caller is an attribute value and
    autoescaping must still run on it.
    """
    words = re.sub(r"<[^>]*>", " ", str(value))
    return " ".join(unescape(words).split())


def build_environment(template_dir: Path | None = None) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(template_dir or TEMPLATE_DIR)),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html", "xml"], default_for_string=True),
        keep_trailing_newline=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["alt_text"] = alt_text
    return env


_ENV: Environment | None = None


def _env() -> Environment:
    global _ENV
    if _ENV is None:
        _ENV = build_environment()
    return _ENV


def template_name(kind: str, lang: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown email kind {kind!r}")
    return f"{kind}.{lang}.html"


def render_html(
    kind: str, lang: str, context: dict[str, Any], *, env: Environment | None = None
) -> str:
    environment = env or _env()
    return environment.get_template(template_name(kind, lang)).render(**context).strip()


def render(
    kind: str,
    lang: str,
    *,
    subject: str,
    text_body: str,
    context: dict[str, Any],
    env: Environment | None = None,
) -> RenderedEmail:
    return RenderedEmail(
        # A subject carrying a newline is a header-injection vector, and
        # the only way one gets here is a value that came off the wire.
        subject=subject.replace("\n", " ").replace("\r", " ").strip(),
        text_body=text_body.strip(),
        html_body=render_html(kind, lang, context, env=env),
    )
