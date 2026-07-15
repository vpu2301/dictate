"""Approved consent-text registry (S11 step 03).

The clinic's approved consent wordings live as files in
``settings.consent_texts_dir`` (repo: ``infra/seeds/consents/``), one per
``(type, version)`` as ``<type>-<version>.md``. The file's exact bytes are
part of the legal record: their sha256 is embedded in the consent's
canonical document, so the КЕП signature binds the precise wording.

A ``(type, version)`` with no file is NOT approved — digital capture is
rejected with 422 ``consent_text_version_unknown``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..config import settings

# Filenames are `<type>-<version>.md`; both parts are identifier-ish so the
# lookup can never traverse paths.
_SAFE_PART = re.compile(r"^[a-z0-9_]+$")


def consent_text_sha256(type_: str, version: str) -> bytes | None:
    """sha256 of the approved text for ``(type, version)``, or None when
    the pair is not in the approved set."""
    if not _SAFE_PART.fullmatch(type_) or not _SAFE_PART.fullmatch(version):
        return None
    path = Path(settings.consent_texts_dir) / f"{type_}-{version}.md"
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).digest()
