"""Token + HMAC helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22}$")


def new_verification_token() -> str:
    """16 random bytes, base64url, no padding (22 chars)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")


def is_well_formed_token(token: str) -> bool:
    """Reject obviously-malformed tokens at the route layer so they
    never touch the DB — defence against path traversal / injection."""
    return bool(_TOKEN_PATTERN.fullmatch(token or ""))


def ipn_hmac(ipn: str, key_hex: str) -> bytes:
    key = bytes.fromhex(key_hex)
    return hmac.new(key, ipn.encode("utf-8"), hashlib.sha256).digest()


def ip_hmac(ip: str, key_hex: str) -> bytes:
    key = bytes.fromhex(key_hex)
    return hmac.new(key, ip.encode("ascii"), hashlib.sha256).digest()
