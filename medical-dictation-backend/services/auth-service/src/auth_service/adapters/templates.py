"""Jinja environment for account mail.

Deliberately the same shape as marketing-service's renderer, because the
two produce mail for the same brand and a divergence would show up in
somebody's inbox rather than in a test.

``StrictUndefined`` is the load-bearing choice: a missing variable in a
password-reset mail is a mail telling a locked-out user to click a link
that goes nowhere. Raising means the row dead-letters and an operator
sees it, instead of the user seeing a broken button.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

TEMPLATE_DIR: Final = Path(__file__).parent / "templates"

# What files actually exist on disk. Declared here rather than imported
# from ``domain.copy`` because the layering runs domain → adapters: an
# adapter reaching back up into domain is the inversion the contract
# exists to prevent, and this layer is the one that owns the files.
#
# The duplication with ``domain.copy.KINDS`` / ``SUPPORTED_LANGS`` is
# deliberate and cheap to police: the parametrised render test iterates
# the domain's vocabulary and would fail the moment the two disagree.
KINDS: Final[tuple[str, ...]] = ("password_reset", "password_changed")
SUPPORTED_LANGS: Final[tuple[str, ...]] = ("en", "de", "uk")


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    text_body: str
    html_body: str


def build_environment(template_dir: Path | None = None) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(template_dir or TEMPLATE_DIR)),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html", "xml"], default_for_string=True),
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


def template_name(kind: str, lang: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown email kind {kind!r}")
    if lang not in SUPPORTED_LANGS:
        raise ValueError(f"unsupported language {lang!r}")
    return f"{kind}.{lang}.html"


def render_html(
    kind: str, lang: str, context: dict[str, Any], *, env: Environment | None = None
) -> str:
    template = (env or _env()).get_template(template_name(kind, lang))
    return template.render(**context).strip()


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
        # A newline in a subject is a header-injection primitive: the
        # bytes after it become additional headers. Stripped here rather
        # than trusted upstream, because this is the last place before
        # the MIME document is assembled.
        subject=subject.replace("\n", " ").replace("\r", " ").strip(),
        text_body=text_body.strip(),
        html_body=render_html(kind, lang, context, env=env),
    )
