"""Short-lived DSAR download tokens (S11 deployment, ADR-0028).

The sprint asks for "presigned downloads at 15-minute TTL". Raw S3
presigned URLs serve envelope ciphertext (platform rule 3) — useless to
the data subject — so the short-TTL property lives on the download LINK
instead: the status endpoint mints an HMAC-signed, expiring token bound
to (tenant, request), and the decrypt-and-stream endpoint refuses
without a fresh one. Auth is still required on top — the token narrows
the window, it never replaces the permission check.

Token format: ``{exp_unix}.{hmac_sha256_hex}`` — stdlib ``hmac`` over a
domain-separated message, same idiom as signing-service's IP-HMAC.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from uuid import UUID

_DOMAIN = "mdx-dsar-download-v1"


def _sig(key_hex: str, tenant_id: UUID, request_id: UUID, exp_unix: int) -> str:
    msg = f"{_DOMAIN}:{tenant_id}:{request_id}:{exp_unix}".encode("ascii")
    return hmac.new(bytes.fromhex(key_hex), msg, hashlib.sha256).hexdigest()


def mint(key_hex: str, *, tenant_id: UUID, request_id: UUID, ttl_seconds: int) -> tuple[str, int]:
    """Return ``(token, exp_unix)`` valid for ``ttl_seconds`` from now."""
    exp_unix = int(time.time()) + ttl_seconds
    return f"{exp_unix}.{_sig(key_hex, tenant_id, request_id, exp_unix)}", exp_unix


def verify(key_hex: str, *, tenant_id: UUID, request_id: UUID, token: str) -> bool:
    """True only for a well-formed, unexpired token with a valid MAC."""
    exp_part, sep, mac_part = token.partition(".")
    if not sep or not exp_part.isdigit():
        return False
    exp_unix = int(exp_part)
    if exp_unix < time.time():
        return False
    return hmac.compare_digest(mac_part, _sig(key_hex, tenant_id, request_id, exp_unix))
