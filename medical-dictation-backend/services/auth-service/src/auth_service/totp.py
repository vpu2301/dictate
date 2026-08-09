"""TOTP (RFC 6238) + envelope packing for the Keycloak-attribute store.

Sprint 16 MFA. Design note (recorded in ADR-0039): Keycloak's admin REST
API cannot register an OTP credential for an existing user, and this
architecture has no Keycloak browser flow (the SPA logs in through
auth-service's direct-grant proxy, sprint A3). So auth-service generates
the secret, stores it **envelope-encrypted in the user's Keycloak
attributes** (Keycloak remains the credential store; no plaintext at
rest anywhere), and validates codes in the login proxy. Keycloak is not
publicly exposed in the production topology — auth-service is the only
token path — so the proxy-side check is enforcement, not decoration.

TOTP itself is pure stdlib ``hmac``/``hashlib`` (RFC 6238 over RFC 4226)
— deliberately NOT ``cryptography.hazmat`` (rule 4 keeps hazmat inside
libs/crypto; an HMAC-based OTP needs no AEAD machinery).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import urllib.parse
from uuid import UUID

from crypto import Envelope, EnvelopeBlob

TOTP_DIGITS = 6
TOTP_PERIOD_SECONDS = 30
TOTP_SECRET_BYTES = 20  # 160-bit, RFC 4226 recommended minimum
# Accept the previous/next step to absorb clock drift; RFC 6238 §5.2.
TOTP_DRIFT_STEPS = 1

# AAD context label — binds the ciphertext to its purpose so a blob
# lifted from the attribute can't be replayed as some other envelope.
_TOTP_AAD_PREFIX = b"mdx-totp-secret-v1:"


def generate_secret() -> str:
    """Fresh base32 (unpadded, upper-case) TOTP secret."""
    return base64.b32encode(os.urandom(TOTP_SECRET_BYTES)).decode("ascii").rstrip("=")


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    """otpauth:// URI for authenticator apps (the SPA renders it as a QR)."""
    label = urllib.parse.quote(f"{issuer}:{account}", safe=":")
    query = urllib.parse.urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": str(TOTP_DIGITS),
            "period": str(TOTP_PERIOD_SECONDS),
        }
    )
    return f"otpauth://totp/{label}?{query}"


def _hotp(key: bytes, counter: int) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**TOTP_DIGITS)).zfill(TOTP_DIGITS)


def totp_at(secret: str, *, at_unix: float | None = None) -> str:
    """The current TOTP code — used by tests and the enrolment self-check."""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    counter = int((time.time() if at_unix is None else at_unix) // TOTP_PERIOD_SECONDS)
    return _hotp(key, counter)


def verify_code(secret: str, code: str, *, at_unix: float | None = None) -> bool:
    """Constant-time TOTP check with ±``TOTP_DRIFT_STEPS`` drift window."""
    code = code.strip().replace(" ", "")
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return False
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    now = time.time() if at_unix is None else at_unix
    counter = int(now // TOTP_PERIOD_SECONDS)
    for step in range(-TOTP_DRIFT_STEPS, TOTP_DRIFT_STEPS + 1):
        if hmac.compare_digest(_hotp(key, counter + step), code):
            return True
    return False


# ── Envelope <-> Keycloak attribute packing ─────────────────────────────
#
# Keycloak attributes are lists of strings; we store one compact JSON
# document with base64url fields. The envelope's AAD binds tenant + sub +
# purpose, so a copied attribute value fails decryption elsewhere.


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


def totp_aad(sub: UUID) -> bytes:
    return _TOTP_AAD_PREFIX + str(sub).encode("ascii")


async def encrypt_secret(
    envelope: Envelope, *, secret: str, tenant_id: UUID, sub: UUID
) -> str:
    blob = await envelope.encrypt(
        secret.encode("ascii"), tenant_id=tenant_id, aad=totp_aad(sub)
    )
    return json.dumps(
        {
            "v": blob.version,
            "alg": blob.algorithm,
            "ct": _b64e(blob.ciphertext),
            "iv": _b64e(blob.iv),
            "tag": _b64e(blob.tag),
            "wdek": _b64e(blob.wrapped_dek),
            "div": _b64e(blob.dek_iv),
            "dtag": _b64e(blob.dek_tag),
            "mkid": blob.master_key_id,
        },
        separators=(",", ":"),
    )


async def decrypt_secret(
    envelope: Envelope, *, packed: str, tenant_id: UUID, sub: UUID
) -> str:
    doc = json.loads(packed)
    blob = EnvelopeBlob(
        ciphertext=_b64d(doc["ct"]),
        iv=_b64d(doc["iv"]),
        tag=_b64d(doc["tag"]),
        wrapped_dek=_b64d(doc["wdek"]),
        dek_iv=_b64d(doc["div"]),
        dek_tag=_b64d(doc["dtag"]),
        tenant_id=tenant_id,
        master_key_id=doc["mkid"],
        algorithm=doc["alg"],
        version=doc["v"],
        extra_aad=totp_aad(sub),
    )
    raw = await envelope.decrypt(blob, tenant_id=tenant_id, aad=totp_aad(sub))
    return raw.decode("ascii")
