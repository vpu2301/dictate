"""Дія.Підпис integration.

The Дія API surface used here:

- ``POST /v1/sign-sessions`` — initiates a session; we POST the
  document hash + display metadata + callback URL.
- ``GET  /v1/sign-sessions/{id}/envelope`` — fetches the signed
  envelope after the user signs.
- ``GET  /v1/health`` — lightweight reachability probe.
- ``GET  /v1/public-keys`` — Дія publishes the public key used to
  sign callbacks; cached locally with 1h TTL.

Callback verification flow:

1. Receive callback. Header ``X-Diia-Signature`` is base64-encoded
   signature over the raw body.
2. Fetch (or use cached) Дія public key.
3. Verify signature; on mismatch raise :class:`InvalidCallbackError`.
4. Parse the body's ``provider_envelope_id``; fetch the envelope.
5. Run ``Envelope(raw).parse()`` to produce :class:`ParsedEnvelope`.

Failures fail closed. The handler never echoes provider response text
into our public surface — only fixed error codes.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from medical_kep.envelope import Envelope, EnvelopeFormat, ParsedEnvelope
from medical_kep.provider import (
    DocumentDisplayMetadata,
    InvalidCallbackError,
    ParsedEnvelopeDTO,
    ProviderHealthSnapshot,
    ProviderName,
    ProviderTransientError,
    SignedEnvelope,
    SignerHint,
    SigningProvider,
    SigningSessionInit,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DiiaConfig:
    base_url: str
    api_token: str
    timeout_s: float = 5.0
    public_keys_ttl_s: int = 3600


class DiiaProvider(SigningProvider):
    name = ProviderName.DIIA

    def __init__(self, config: DiiaConfig, *, http: httpx.AsyncClient | None = None) -> None:
        self._cfg = config
        self._http = http or httpx.AsyncClient(
            timeout=config.timeout_s,
            headers={"Authorization": f"Bearer {config.api_token}"},
        )
        self._pubkeys_cache: tuple[float, list[bytes]] | None = None

    async def initiate(
        self,
        *,
        document_pdf_hash: bytes,
        display: DocumentDisplayMetadata,
        signer_hint: SignerHint | None,
        callback_url: str,
    ) -> SigningSessionInit:
        body = {
            "document_hash_sha256": base64.b64encode(document_pdf_hash).decode("ascii"),
            "display": {
                "title": display.title,
                "code": display.report_code,
                "issuer": display.issuer_name,
                "encounter_date": display.encounter_date_iso,
                "language": display.language,
            },
            "callback_url": callback_url,
            "signer_hint": (
                {"full_name": signer_hint.full_name} if signer_hint and signer_hint.full_name else None
            ),
        }
        try:
            r = await self._http.post(f"{self._cfg.base_url}/v1/sign-sessions", json=body)
        except httpx.HTTPError as exc:
            raise ProviderTransientError(f"Дія unreachable: {exc.__class__.__name__}") from exc
        if r.status_code >= 500:
            raise ProviderTransientError(f"Дія 5xx: {r.status_code}")
        if r.status_code >= 400:
            raise InvalidCallbackError(f"Дія init rejected: {r.status_code}")
        data = r.json()
        expires_at = datetime.fromisoformat(data["expires_at"]) if "expires_at" in data else (
            datetime.now(timezone.utc) + timedelta(minutes=10)
        )
        return SigningSessionInit(
            provider=ProviderName.DIIA,
            provider_session_id=data["session_id"],
            expires_at=expires_at,
            redirect_url=data.get("redirect_url"),
            qr_payload=data.get("qr_payload"),
        )

    async def handle_callback(
        self,
        *,
        provider_session_id: str,
        callback_body: bytes,
        callback_headers: dict[str, str],
    ) -> SignedEnvelope:
        sig_b64 = callback_headers.get("X-Diia-Signature", "")
        if not sig_b64:
            raise InvalidCallbackError("missing X-Diia-Signature header")
        try:
            sig = base64.b64decode(sig_b64)
        except Exception as exc:  # noqa: BLE001
            raise InvalidCallbackError("malformed signature header") from exc

        pubkeys = await self._fetch_public_keys()
        if not _verify_with_any(callback_body, sig, pubkeys):
            raise InvalidCallbackError("Дія callback signature invalid")

        try:
            payload = json.loads(callback_body)
        except Exception as exc:  # noqa: BLE001
            raise InvalidCallbackError("Дія callback body not JSON") from exc

        envelope_id = payload.get("envelope_id")
        if not envelope_id:
            raise InvalidCallbackError("Дія callback missing envelope_id")

        try:
            r = await self._http.get(
                f"{self._cfg.base_url}/v1/sign-sessions/{provider_session_id}/envelope"
            )
        except httpx.HTTPError as exc:
            raise ProviderTransientError(f"Дія envelope fetch unreachable: {exc}") from exc
        if r.status_code != 200:
            raise InvalidCallbackError(f"Дія envelope fetch {r.status_code}")
        raw = r.content
        parsed = Envelope(raw, declared_format=EnvelopeFormat.PADES_LTV).parse()
        return SignedEnvelope(
            provider=ProviderName.DIIA,
            provider_envelope_id=envelope_id,
            signed_bytes=raw,
            parsed=ParsedEnvelopeDTO(
                signer_full_name=parsed.signer_full_name,
                signer_ipn=parsed.signer_ipn,
                signer_cert_serial=parsed.signer_cert_serial_hex,
                signer_cert_issuer_cn=parsed.signer_cert_issuer_cn,
                cert_chain_pem=parsed.cert_chain_pem,
                document_hash_sha256=parsed.document_hash_sha256,
                signed_at=parsed.signed_at,
                tsa_token_present=parsed.tsa_token_present,
                ocsp_responses_present=parsed.ocsp_responses_present,
                signature_algorithm=parsed.signature_algorithm,
                is_qualified=parsed.is_qualified,
                format=parsed.format.value,
            ),
        )

    async def health(self) -> ProviderHealthSnapshot:
        t0 = time.monotonic()
        try:
            r = await self._http.get(f"{self._cfg.base_url}/v1/health")
        except httpx.HTTPError as exc:
            return ProviderHealthSnapshot(
                provider=ProviderName.DIIA,
                healthy=False,
                latency_ms=int((time.monotonic() - t0) * 1000),
                last_error=str(exc.__class__.__name__),
            )
        return ProviderHealthSnapshot(
            provider=ProviderName.DIIA,
            healthy=(r.status_code == 200),
            latency_ms=int((time.monotonic() - t0) * 1000),
            last_error=None if r.status_code == 200 else f"http_{r.status_code}",
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── public-key cache ────────────────────────────────────────────

    async def _fetch_public_keys(self) -> list[bytes]:
        now = time.monotonic()
        if self._pubkeys_cache is not None:
            cached_at, keys = self._pubkeys_cache
            if now - cached_at < self._cfg.public_keys_ttl_s:
                return keys
        try:
            r = await self._http.get(f"{self._cfg.base_url}/v1/public-keys")
        except httpx.HTTPError as exc:
            raise ProviderTransientError(
                f"Дія public-keys unreachable: {exc.__class__.__name__}"
            ) from exc
        if r.status_code != 200:
            raise ProviderTransientError(f"Дія public-keys {r.status_code}")
        data = r.json()
        # The endpoint returns {"keys": ["-----BEGIN PUBLIC KEY-----\n..."]}.
        pems = [k.encode("ascii") for k in data.get("keys", [])]
        self._pubkeys_cache = (now, pems)
        return pems


def _verify_with_any(body: bytes, sig: bytes, pem_keys: list[bytes]) -> bool:
    for pem in pem_keys:
        try:
            pk = serialization.load_pem_public_key(pem)
            try:
                pk.verify(sig, body, padding.PKCS1v15(), hashes.SHA256())
                return True
            except InvalidSignature:
                continue
        except Exception:  # noqa: BLE001
            continue
    return False
