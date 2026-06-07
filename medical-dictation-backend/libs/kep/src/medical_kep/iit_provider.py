"""ІІТ provider.

ІІТ's UX is smart-card-based; the FE talks to a local helper (browser
extension / WebUSB) that interacts with the user's smart card. The
signed envelope is then POSTed back to our callback endpoint.

Authentication of the callback is via system HMAC (sprint-09 ships a
single key; sprint-17 will rotate per-tenant). The HMAC is computed
over the raw body using the shared key.

Envelope shape is the same CMS SignedData that Дія returns, so the
verification path (envelope.py + verify.py) is unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from medical_kep.envelope import Envelope, EnvelopeFormat
from medical_kep.provider import (
    DocumentDisplayMetadata,
    InvalidCallbackError,
    ParsedEnvelopeDTO,
    ProviderHealthSnapshot,
    ProviderName,
    SignedEnvelope,
    SignerHint,
    SigningProvider,
    SigningSessionInit,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IitConfig:
    helper_health_url: str
    callback_hmac_key: bytes
    helper_protocol_version: str = "1.0"
    timeout_s: float = 5.0


class IitProvider(SigningProvider):
    name = ProviderName.IIT

    def __init__(self, config: IitConfig, *, http: httpx.AsyncClient | None = None) -> None:
        self._cfg = config
        self._http = http or httpx.AsyncClient(timeout=config.timeout_s)

    async def initiate(
        self,
        *,
        document_pdf_hash: bytes,
        display: DocumentDisplayMetadata,
        signer_hint: SignerHint | None,
        callback_url: str,
    ) -> SigningSessionInit:
        # ІІТ initiate is FE-driven: we don't open a remote session.
        # We hand the FE a "local_helper_payload" the helper consumes.
        payload = {
            "protocol_version": self._cfg.helper_protocol_version,
            "document_hash_b64": base64.b64encode(document_pdf_hash).decode("ascii"),
            "display": {
                "title": display.title,
                "code": display.report_code,
                "issuer": display.issuer_name,
                "encounter_date": display.encounter_date_iso,
                "language": display.language,
            },
            "callback_url": callback_url,
        }
        return SigningSessionInit(
            provider=ProviderName.IIT,
            provider_session_id=f"iit-{int(time.time() * 1000)}-{hashlib.sha256(document_pdf_hash).hexdigest()[:8]}",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            local_helper_payload=payload,
        )

    async def handle_callback(
        self,
        *,
        provider_session_id: str,
        callback_body: bytes,
        callback_headers: dict[str, str],
    ) -> SignedEnvelope:
        sig_hex = callback_headers.get("X-IIT-HMAC", "")
        expected = hmac.new(self._cfg.callback_hmac_key, callback_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig_hex.encode("ascii"), expected.encode("ascii")):
            raise InvalidCallbackError("ІІТ callback HMAC invalid")
        try:
            payload = json.loads(callback_body)
        except Exception as exc:  # noqa: BLE001
            raise InvalidCallbackError("ІІТ callback body not JSON") from exc
        if not payload.get("approved", False):
            raise InvalidCallbackError("ІІТ signing rejected by user")
        try:
            raw = base64.b64decode(payload["envelope_b64"])
        except Exception as exc:  # noqa: BLE001
            raise InvalidCallbackError("ІІТ envelope_b64 malformed") from exc
        envelope_id = payload.get("envelope_id") or f"iit-env-{provider_session_id}"
        parsed = Envelope(raw, declared_format=EnvelopeFormat.CADES_T).parse()
        return SignedEnvelope(
            provider=ProviderName.IIT,
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
            r = await self._http.get(self._cfg.helper_health_url)
        except httpx.HTTPError as exc:
            return ProviderHealthSnapshot(
                provider=ProviderName.IIT,
                healthy=False,
                latency_ms=int((time.monotonic() - t0) * 1000),
                last_error=str(exc.__class__.__name__),
            )
        return ProviderHealthSnapshot(
            provider=ProviderName.IIT,
            healthy=(r.status_code == 200),
            latency_ms=int((time.monotonic() - t0) * 1000),
            last_error=None if r.status_code == 200 else f"http_{r.status_code}",
        )

    async def aclose(self) -> None:
        await self._http.aclose()
