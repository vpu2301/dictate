"""Trust store: bundled CA certs + TSA roots used by ``verify_envelope``.

Sprint-09 ships a file-system-backed trust store. ``infra/trust-store/``
contains:

- ``ca-bundle.pem`` — concatenated PEM blocks for trusted root CAs.
- ``tsa-bundle.pem`` — concatenated PEM blocks for trusted TSA roots.
- ``czo-cert.pem`` — the central certification authority public cert
  (used to verify weekly TSL downloads).

The bundle is loaded at service startup. Refresh requires a service
restart (PR-gated by ADR-0023's discussion of trust-store change
governance).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from asn1crypto import x509

logger = logging.getLogger(__name__)


class TrustStoreError(Exception):
    pass


@dataclass(slots=True)
class TrustStore:
    ca_certs: list[x509.Certificate]
    tsa_certs: list[x509.Certificate]
    czo_cert: x509.Certificate | None = None
    test_ca_certs: list[x509.Certificate] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.test_ca_certs is None:
            object.__setattr__(self, "test_ca_certs", [])

    @classmethod
    def load_from_dir(cls, dir_path: Path, *, include_test_ca: bool = False) -> "TrustStore":
        ca_path = dir_path / "ca-bundle.pem"
        tsa_path = dir_path / "tsa-bundle.pem"
        czo_path = dir_path / "czo-cert.pem"
        test_ca_path = dir_path / "test-ca-bundle.pem"

        ca_certs = _load_pem_bundle(ca_path) if ca_path.exists() else []
        tsa_certs = _load_pem_bundle(tsa_path) if tsa_path.exists() else []
        czo_cert = _load_pem_bundle(czo_path)[0] if czo_path.exists() else None

        test_ca_certs: list[x509.Certificate] = []
        if include_test_ca and test_ca_path.exists():
            test_ca_certs = _load_pem_bundle(test_ca_path)
            if test_ca_certs:
                logger.warning(
                    "trust_store.test_ca_loaded — NEVER allowed in production"
                )

        return cls(
            ca_certs=ca_certs,
            tsa_certs=tsa_certs,
            czo_cert=czo_cert,
            test_ca_certs=test_ca_certs,
        )

    def all_anchors(self) -> list[x509.Certificate]:
        return [*self.ca_certs, *self.test_ca_certs]

    def is_anchor(self, cert: x509.Certificate) -> bool:
        return any(
            anchor.subject == cert.subject and anchor.serial_number == cert.serial_number
            for anchor in self.all_anchors()
        )

    def is_test_anchor(self, cert: x509.Certificate) -> bool:
        return any(
            anchor.subject == cert.subject and anchor.serial_number == cert.serial_number
            for anchor in self.test_ca_certs
        )

    def find_by_subject(self, subject: x509.Name) -> x509.Certificate | None:
        for c in self.all_anchors():
            if c.subject == subject:
                return c
        return None


def _load_pem_bundle(path: Path) -> list[x509.Certificate]:
    try:
        text = path.read_text("utf-8")
    except OSError as exc:
        raise TrustStoreError(f"cannot read {path}: {exc}") from exc

    blocks: list[str] = []
    cur: list[str] = []
    in_block = False
    for line in text.splitlines():
        if "BEGIN CERTIFICATE" in line:
            in_block = True
            cur = [line]
        elif "END CERTIFICATE" in line:
            cur.append(line)
            blocks.append("\n".join(cur))
            in_block = False
            cur = []
        elif in_block:
            cur.append(line)

    out: list[x509.Certificate] = []
    for b in blocks:
        try:
            from cryptography.hazmat.primitives import serialization

            der_or_pem = b.encode("ascii")
            crypto_cert = serialization.load_pem_x509_certificate.__self__ if False else None  # noqa: F841 — placeholder
            # Use asn1crypto on the DER bytes for consistency with envelope.py.
            import base64

            inner = "\n".join(
                line for line in b.splitlines() if "CERTIFICATE" not in line
            )
            der = base64.b64decode(inner)
            out.append(x509.Certificate.load(der))
        except Exception as exc:  # noqa: BLE001
            raise TrustStoreError(f"failed to parse cert in {path}: {exc}") from exc
    return out
