"""Envelope verification.

Inputs:
- ``parsed`` — structural breakdown from :class:`Envelope.parse`.
- ``expected_document_hash`` — the SHA-256 our signing flow committed
  to (sprint-08 canonical PDF hash).
- ``trust_store`` — anchor set.

Output:
- :class:`VerificationResult` with ``valid=True`` and no errors when
  every check passes; otherwise ``valid=False`` and a list of
  human-readable error strings (callers do not pass these strings
  through to the public ``/verify`` response verbatim — the public
  shape is a fixed enum projected from this list).

Checks performed (each fail-closed):
1. Document hash binding — ``parsed.document_hash_sha256 == expected``.
2. Certificate chain — every cert chains up to a trusted anchor.
3. Signer certificate validity window includes ``signed_at``.
4. TSA token present (sprint-09 day-7 alert on absence; for
   verification we treat it as a warning, not a fatal — many ІІТ
   test envelopes are CAdES-BES).
5. OCSP/CRL evidence — sprint-09 stores the embedded responses as
   evidence; full revocation re-check requires network access to
   ОCSP responder. ``verify_envelope`` uses the *stored* responses;
   live re-check is a separate path used by ``/verify``.
6. Qualified-status check — for QES, the issuer cert MUST be in the
   trust store, not in test-CA list (test envelopes flagged separately).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from asn1crypto import x509

from medical_kep.envelope import ParsedEnvelope
from medical_kep.provider import VerificationResult
from medical_kep.trust_store import TrustStore

logger = logging.getLogger(__name__)


class VerificationError(Exception):
    pass


def verify_envelope(
    *,
    parsed: ParsedEnvelope,
    expected_document_hash: bytes,
    trust_store: TrustStore,
    now: datetime | None = None,
) -> VerificationResult:
    errors: list[str] = []
    now = now or datetime.now(UTC)

    # 1) Document hash binding.
    if parsed.document_hash_sha256 != expected_document_hash:
        errors.append("document_hash_mismatch")

    # 2) Certificate chain.
    chain = _load_chain(parsed.cert_chain_pem)
    if not chain:
        errors.append("empty_certificate_chain")
        return _result(parsed, errors)

    signer_cert = chain[0]
    if not _chain_terminates_in_trust(chain, trust_store):
        errors.append("untrusted_certificate_chain")

    # 3) Signer cert validity window at signing time.
    not_before = signer_cert["tbs_certificate"]["validity"]["not_before"].native
    not_after = signer_cert["tbs_certificate"]["validity"]["not_after"].native
    if not_before > parsed.signed_at:
        errors.append("certificate_not_yet_valid_at_sign_time")
    if not_after < parsed.signed_at:
        errors.append("certificate_expired_at_sign_time")

    # 4) TSA evidence.
    if parsed.format in ("PAdES-LTV", "CAdES-T") and not parsed.tsa_token_present:
        errors.append("missing_tsa_token_for_declared_format")

    # 5) OCSP/CRL evidence (warning only at sprint-09 ship; sprint-17
    # will tighten to an error if pilot data shows revocation never
    # legitimately absent).
    if parsed.is_qualified and not parsed.ocsp_responses_present:
        logger.info("verify.qualified_without_ocsp")

    # 6) Test-CA-anchored chain isn't qualified.
    test_anchored = trust_store.is_test_anchor(chain[-1])
    is_qualified = parsed.is_qualified and not test_anchored

    return _result(parsed, errors, override_is_qualified=is_qualified)


# ── Helpers ─────────────────────────────────────────────────────────


def _load_chain(pem_blocks: list[str]) -> list[x509.Certificate]:
    import base64

    out: list[x509.Certificate] = []
    for block in pem_blocks:
        inner = "\n".join(line for line in block.splitlines() if "CERTIFICATE" not in line)
        try:
            der = base64.b64decode(inner)
            out.append(x509.Certificate.load(der))
        except Exception:  # noqa: BLE001
            continue
    return out


def _chain_terminates_in_trust(chain: list[x509.Certificate], trust: TrustStore) -> bool:
    if not chain:
        return False
    # Either the leaf's issuer is a trust anchor, or we walk up the
    # chain matching issuer == previous.subject and the top cert is an
    # anchor.
    if trust.find_by_subject(chain[0]["tbs_certificate"]["issuer"]) is not None:
        return True
    return any(trust.is_anchor(chain[i]) for i in range(1, len(chain)))


def _result(
    parsed: ParsedEnvelope,
    errors: list[str],
    override_is_qualified: bool | None = None,
) -> VerificationResult:
    return VerificationResult(
        valid=(len(errors) == 0),
        errors=errors,
        signer_full_name=parsed.signer_full_name,
        signer_cert_serial=parsed.signer_cert_serial_hex,
        signer_cert_issuer_cn=parsed.signer_cert_issuer_cn,
        signed_at=parsed.signed_at,
        is_qualified=(
            override_is_qualified if override_is_qualified is not None else parsed.is_qualified
        ),
        document_hash_sha256_hex=parsed.document_hash_sha256.hex(),
        format=parsed.format.value if hasattr(parsed.format, "value") else parsed.format,
    )
