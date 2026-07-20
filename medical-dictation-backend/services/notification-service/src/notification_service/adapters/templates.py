"""Jinja2 email rendering.

Three properties this module must hold, in priority order:

1. **PHI-free.** A template can only see the variables handed to it, and
   the only source of those is `domain/render.safe_payload`, which is a
   per-category allow-list. There is no `**event.payload` splat
   anywhere, so a producer cannot widen what a template can reach.
2. **Autoescaped.** Report codes and provider names are attacker-
   influenced in principle; unescaped they are an HTML-injection vector
   in a mail client (the sprint-09 lesson).
3. **Deterministic.** The same input renders byte-identical output, so
   the PHI CI gate and the round-trip test can assert on exact bytes.
   Nothing here reads a clock or a random source.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from notification_events import Category

TEMPLATE_DIR = Path(__file__).parent / "templates"


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    text_body: str
    html_body: str


def build_environment(template_dir: Path | None = None) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(template_dir or TEMPLATE_DIR)),
        # StrictUndefined: a typo'd variable must fail the render, not
        # quietly produce "Report  has been signed".
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html", "xml"]),
        # Deterministic output — no trailing-newline drift between runs.
        keep_trailing_newline=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


_ENV: Environment | None = None


def _env() -> Environment:
    global _ENV
    if _ENV is None:
        _ENV = build_environment()
    return _ENV


def render_email(
    category: Category,
    *,
    template_stem: str,
    fields: dict[str, Any],
    deep_link: str,
    env: Environment | None = None,
    items: list[str] | None = None,
) -> RenderedEmail:
    """Render one category's mail.

    `fields` MUST already be the allow-listed projection — this function
    does not filter, it only renders, and passing a raw payload here
    would defeat the boundary.

    `items` is the digest's per-line summary. It is a separate parameter
    rather than a `fields` key so it cannot be populated from an event
    payload — only the digest job, which builds the lines from already-
    rendered PHI-free titles, can supply it.
    """
    environment = env or _env()
    context = {
        "fields": fields,
        "deep_link": deep_link,
        "category": str(category),
        "items": items or [],
    }

    subject = environment.get_template(f"{template_stem}.subject.txt").render(**context)
    text = environment.get_template(f"{template_stem}.txt").render(**context)
    html = environment.get_template(f"{template_stem}.html").render(**context)

    # A subject with a newline is a header-injection vector.
    return RenderedEmail(
        subject=subject.replace("\n", " ").replace("\r", " ").strip(),
        text_body=text.strip(),
        html_body=html.strip(),
    )
