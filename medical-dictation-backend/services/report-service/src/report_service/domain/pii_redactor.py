"""Best-effort PII redaction for search snippets.

Sprint-08: clinical content sometimes leaks patient identifiers into
section bodies (name, IPN, DOB). When a snippet is returned to a user
who is NOT on the treatment team, redact these patterns. When the
viewer is primary_author / co_author / admin, return unredacted.

The redactor is intentionally conservative: it is the second line of
defence behind the role check; clinical content lead reviews quality
each release.
"""

from __future__ import annotations

import re
from typing import Final

# 10-digit IPN (Ukraine).
_IPN_RE: Final = re.compile(r"\b\d{10}\b")
# Ukrainian-typical PIB three-word capitalised pattern (Иванов Иван Иванович).
_PIB_RE: Final = re.compile(
    r"\b([А-ЩЬЮЯҐЄІЇA-Z][а-щьюяґєіїa-z]{1,})\s+"
    r"([А-ЩЬЮЯҐЄІЇA-Z][а-щьюяґєіїa-z]{1,})\s+"
    r"([А-ЩЬЮЯҐЄІЇA-Z][а-щьюяґєіїa-z]{1,})\b",
    re.UNICODE,
)
# ISO date or "12.05.1980" style — only redact full year birthdates,
# not encounter dates. Best-effort.
_DOB_LIKE_RE: Final = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b")


def redact_snippet(text: str) -> str:
    text = _IPN_RE.sub("[redacted-ipn]", text)
    text = _PIB_RE.sub("[redacted-name]", text)
    text = _DOB_LIKE_RE.sub("[redacted-date]", text)
    return text


def is_treatment_team(
    *, viewer_user_id, primary_author_id, co_author_ids, viewer_roles: list[str]
) -> bool:
    """Treatment-team check used to bypass redaction.

    `tenant_admin` and `dpo` see unredacted snippets (audit duty).
    """
    if viewer_user_id == primary_author_id:
        return True
    if viewer_user_id in (co_author_ids or []):
        return True
    if "tenant_admin" in (viewer_roles or []):
        return True
    if "dpo" in (viewer_roles or []):
        return True
    return False
