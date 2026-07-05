"""Real-UAPKI integration — gated on RUN_UAPKI_INTEGRATION=1 (Linux).

Exercises the production ctypes backend against the vendored DSTU test
container (``tests/fixtures/uapki/test-diia.p12``, password
``testpassword``): sign canonical bytes detached, verify VALID, tamper
one byte → INVALID, wrong password → InvalidCredentialsError.

Requires ``UAPKI_LIB_DIR`` pointing at the extracted UAPKI release
(``libuapki.so.2`` + ``libcm-pkcs12.so`` + deps) — see ADR-0026.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from medical_kep import InvalidCredentialsError, UapkiBackend, UapkiConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_UAPKI_INTEGRATION") != "1",
    reason="set RUN_UAPKI_INTEGRATION=1 (Linux + UAPKI_LIB_DIR) to run",
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "uapki"
CANONICAL = b'{"canonical_version":"1.0","probe":"uapki-integration"}'


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> UapkiBackend:
    lib_dir = Path(os.environ.get("UAPKI_LIB_DIR", "/opt/uapki"))
    if not (lib_dir / "libuapki.so.2").exists() and not (lib_dir / "libuapki.so").exists():
        pytest.skip(f"UAPKI libs not found at {lib_dir}")
    return UapkiBackend(
        UapkiConfig(
            lib_dir=lib_dir,
            cert_cache_dir=FIXTURES / "certs",
            crl_cache_dir=FIXTURES / "crls",
            tsp_url=None,
            offline=True,
        )
    )


def test_sign_and_verify_dstu_roundtrip(backend: UapkiBackend) -> None:
    container = (FIXTURES / "test-diia.p12").read_bytes()
    result = backend.sign_detached(
        container=container, password="testpassword", data=CANONICAL
    )
    assert result.sign_algo_oid.startswith("1.2.804.")  # DSTU 4145 family
    assert result.signature_der[:1] == b"\x30"

    verdict = backend.verify_detached(signature_der=result.signature_der, data=CANONICAL)
    assert verdict.signature_status == "VALID"
    assert verdict.message_digest_status == "VALID"

    tampered = bytearray(CANONICAL)
    tampered[0] ^= 0x01
    bad = backend.verify_detached(
        signature_der=result.signature_der, data=bytes(tampered)
    )
    assert bad.message_digest_status != "VALID"
    assert bad.total_valid is False


def test_wrong_password_raises_invalid_credentials(backend: UapkiBackend) -> None:
    container = (FIXTURES / "test-diia.p12").read_bytes()
    with pytest.raises(InvalidCredentialsError):
        backend.sign_detached(container=container, password="wrong", data=CANONICAL)
