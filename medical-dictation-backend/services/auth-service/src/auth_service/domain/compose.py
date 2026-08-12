"""Turn "we decided to mail this person" into the variables a template needs.

Everything a template will interpolate is computed here, at request
time, and frozen into the outbox row. The renderer that runs later gets
a plain dict and makes no decisions — which is what makes a queued mail
reproducible: the row records exactly what will be said, not a recipe
that could produce something different once settings change.

The one structural rule this module exists to enforce is the split
between :func:`render_fields` and :func:`secret_fields`. Only the second
carries a redeemable token, and only the second is destroyed once the
mail is sent. See migration 0076 for the CHECK constraint that keeps
that from being merely a convention.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Final
from urllib.parse import quote

from . import copy as copy_mod

# Shown when the request carries no usable User-Agent. A blank row reads
# like missing data and invites the reader to distrust the whole mail,
# which is the last thing a security notification can afford.
_UNKNOWN_CLIENT: Final[dict[str, str]] = {
    "en": "Unrecognised device",
    "de": "Unbekanntes Gerät",
    "uk": "Невідомий пристрій",
}

# Coarse, deliberately lossy User-Agent parsing. We want "Chrome on
# macOS", not a fingerprint: the point is to let the account holder
# recognise their own device, and a full UA string in an email is both
# unreadable and a gift to anyone who gets hold of the mailbox.
_BROWSERS: Final[tuple[tuple[str, str], ...]] = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Chrome/", "Chrome"),
    ("Firefox/", "Firefox"),
    ("Safari/", "Safari"),
)
_PLATFORMS: Final[tuple[tuple[str, str], ...]] = (
    ("Windows", "Windows"),
    ("Macintosh", "macOS"),
    ("Mac OS X", "macOS"),
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("Linux", "Linux"),
)

_ON: Final[dict[str, str]] = {"en": "on", "de": "auf", "uk": "на"}


def client_label(user_agent: str, lang: str) -> str:
    """A short, human-recognisable description of the requesting client."""
    ua = (user_agent or "").strip()
    if not ua:
        return _UNKNOWN_CLIENT.get(lang, _UNKNOWN_CLIENT["en"])
    browser = next((name for token, name in _BROWSERS if token in ua), "")
    platform = next((name for token, name in _PLATFORMS if token in ua), "")
    if browser and platform:
        return f"{browser} {_ON.get(lang, 'on')} {platform}"
    if browser or platform:
        return browser or platform
    return _UNKNOWN_CLIENT.get(lang, _UNKNOWN_CLIENT["en"])


def hash_ip(ip: str, *, salt: str) -> str:
    """Salted, truncated hash of a client IP.

    Truncated to 128 bits because the full digest buys nothing here and
    the shorter value is easier to eyeball in an incident. Salted
    because the IPv4 space is small enough to reverse an unsalted hash
    by brute force in seconds — an unsalted "anonymised" IP is just an
    IP with extra steps.
    """
    if not ip:
        return ""
    digest = hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()
    return digest[:32]


def _sanitise_base(base_url: str) -> str:
    return (base_url or "").rstrip("/")


def reset_url(*, app_base_url: str, token: str) -> str:
    """Deep link into the SPA's reset screen.

    The token rides in the hash fragment's query, matching the SPA's
    hash router. That placement is also the safer one: a fragment is
    never sent to the server, so the token stays out of access logs on
    every proxy between the clinic and the app, and out of the Referer
    header when the reset page loads a third-party resource.
    """
    return f"{_sanitise_base(app_base_url)}/#/reset-password?token={quote(token, safe='')}"


def lockdown_url(*, app_base_url: str, token: str) -> str:
    """Deep link into the SPA's "that wasn't me" screen."""
    return (
        f"{_sanitise_base(app_base_url)}/#/account-recovery?token={quote(token, safe='')}"
    )


def privacy_url(*, app_base_url: str) -> str:
    return f"{_sanitise_base(app_base_url)}/#/legal/privacy"


def _base_fields(
    *,
    lang: str,
    email: str,
    display_name: str,
    user_agent: str,
    support_url: str,
    app_base_url: str,
) -> dict[str, Any]:
    return {
        "greeting": copy_mod.greeting(lang, display_name),
        "email": email,
        "client_label": client_label(user_agent, lang),
        "support_url": support_url,
        "privacy_url": privacy_url(app_base_url=app_base_url),
    }


def password_reset_fields(
    *,
    lang: str,
    email: str,
    display_name: str,
    user_agent: str,
    support_url: str,
    app_base_url: str,
    requested_at: Any,
    ttl_seconds: int,
) -> dict[str, Any]:
    """Non-secret variables for the reset mail. Safe to retain."""
    fields = _base_fields(
        lang=lang,
        email=email,
        display_name=display_name,
        user_agent=user_agent,
        support_url=support_url,
        app_base_url=app_base_url,
    )
    fields["requested_at"] = copy_mod.format_moment(requested_at, lang)
    fields["expiry_label"] = copy_mod.minutes_label(ttl_seconds, lang)
    return fields


def password_changed_fields(
    *,
    lang: str,
    email: str,
    display_name: str,
    user_agent: str,
    support_url: str,
    app_base_url: str,
    changed_at: Any,
    lockdown_ttl_seconds: int,
) -> dict[str, Any]:
    """Non-secret variables for the security notification."""
    fields = _base_fields(
        lang=lang,
        email=email,
        display_name=display_name,
        user_agent=user_agent,
        support_url=support_url,
        app_base_url=app_base_url,
    )
    fields["changed_at"] = copy_mod.format_moment(changed_at, lang)
    fields["lockdown_expiry_label"] = _days_label(lockdown_ttl_seconds, lang)
    return fields


def _days_label(seconds: int, lang: str) -> str:
    """"7 days" / "7 Tage" / "7 днів"."""
    days = max(1, round(seconds / 86400))
    if lang == "de":
        return f"{days} Tag" if days == 1 else f"{days} Tage"
    if lang == "uk":
        tail_two = days % 100
        tail_one = days % 10
        if 11 <= tail_two <= 14:
            word = "днів"
        elif tail_one == 1:
            word = "день"
        elif 2 <= tail_one <= 4:
            word = "дні"
        else:
            word = "днів"
        return f"{days} {word}"
    return f"{days} day" if days == 1 else f"{days} days"


def reset_secret_fields(*, app_base_url: str, token: str) -> dict[str, Any]:
    """The token-bearing half. Cleared the moment the mail is sent."""
    return {"reset_url": reset_url(app_base_url=app_base_url, token=token)}


def lockdown_secret_fields(*, app_base_url: str, token: str) -> dict[str, Any]:
    return {"lockdown_url": lockdown_url(app_base_url=app_base_url, token=token)}


def text_values(
    kind: str, lang: str, fields: dict[str, Any], secrets: dict[str, Any]
) -> dict[str, str]:
    """Flatten both halves into the ``str.format`` inputs the text body wants.

    The text template also needs a ``client_line`` that collapses to
    nothing when there is no client to describe — computed here rather
    than stored, so the stored row keeps the raw label and the
    presentation stays in one place.
    """
    merged: dict[str, str] = {k: str(v) for k, v in {**fields, **secrets}.items()}
    label = merged.get("client_label", "")
    unknown = set(_UNKNOWN_CLIENT.values())
    merged["client_line"] = copy_mod.client_line(
        lang, "" if label in unknown else label
    )
    return merged


_EMAIL_RE: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def looks_like_email(value: str) -> bool:
    """Cheap shape check for the unauthenticated request endpoint.

    Not validation — Pydantic's ``EmailStr`` does that. This exists so
    the rate-limit key for a malformed address cannot be an unbounded
    attacker-chosen string.
    """
    return bool(_EMAIL_RE.match((value or "").strip()))
