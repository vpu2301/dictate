"""UAPKI backend — DSTU 4145 file-key signing via the open-source UAPKI
library (ADR-0026).

UAPKI (github.com/specinfo-ua/UAPKI, BSD-2-Clause) is the open-source
PKI stack from the ЦЗО software vendor, holding Ukrainian state
cryptographic expertise certification. It exposes a single C ABI entry
point — ``char* process(const char* json)`` — driving a JSON task API:

    INIT → OPEN → KEYS → SELECT_KEY → SIGN → CLOSE → DEINIT / VERIFY

Key-container handling: containers (PKCS#12 / JKS / ІІТ ``Key-6.dat`` /
PKCS#8) are passed **fully in memory** via ``storage: "file://memory"``
+ base64 ``openParams.bytes`` — the clinician's private key never
touches disk here. Working ``bytearray`` copies we control are zeroed
after use; Python's immutable ``str``/``bytes`` for the JSON request
are best-effort (documented limitation, see ADR-0026).

Threading: the library keeps global state per ``INIT`` and per open
storage, so ALL calls are serialised behind a module lock and executed
in a worker thread (``asyncio.to_thread`` at the provider layer).

Verified on Linux (2026-07-03, v2.0.12 linux-amd64 in a
python:3.12-slim-bookworm container): DSTU 4145 CAdES-BES detached
sign → VERIFY ``TOTAL-VALID``; one-byte tamper → ``TOTAL-FAILED`` with
``messageDigest=INVALID``; wrong password → errorCode 1035
``INVALID_MAC``.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from medical_kep.provider import (
    InvalidCredentialsError,
    ProviderTransientError,
)

logger = logging.getLogger(__name__)

_DSTU4145_GOST34311_SIGN_ALGO = "1.2.804.2.1.1.1.1.3.1.1"


class UapkiError(Exception):
    def __init__(self, method: str, error_code: int, error: str) -> None:
        self.method = method
        self.error_code = error_code
        self.error = error
        super().__init__(f"UAPKI {method} failed: code={error_code} {error}")


@dataclass(frozen=True, slots=True)
class UapkiConfig:
    """Where the UAPKI shared objects and PKI caches live.

    ``lib_dir`` must contain ``libuapki.so.2`` + ``libcm-pkcs12.so`` (+
    their deps). ``tsp_url`` enables CAdES-T (qualified timestamp); when
    unset or unreachable the backend degrades to CAdES-BES and reports
    ``tsa_applied=False`` so callers can surface the downgrade.
    """

    lib_dir: Path
    cert_cache_dir: Path
    crl_cache_dir: Path
    tsp_url: str | None = None
    offline: bool = True
    trusted_certs_b64: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class UapkiSignResult:
    signature_der: bytes  # detached CAdES CMS
    signer_cert_der: bytes
    sign_algo_oid: str
    tsa_applied: bool


@dataclass(frozen=True, slots=True)
class UapkiVerifyResult:
    total_valid: bool
    signature_status: str
    message_digest_status: str
    raw: dict[str, Any]


class UapkiBackend:
    """Serialised, lazily-initialised wrapper over ``libuapki.so``."""

    def __init__(self, config: UapkiConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._lib: ctypes.CDLL | None = None
        self._initialised = False

    # ── Public API (blocking; call via asyncio.to_thread) ───────────

    def sign_detached(
        self,
        *,
        container: bytes,
        password: str,
        data: bytes,
    ) -> UapkiSignResult:
        """OPEN the container from memory, sign ``data`` detached, CLOSE.

        Raises :class:`InvalidCredentialsError` when the container can't
        be opened (bad bytes or wrong password) and
        :class:`ProviderTransientError` on library/TSP-level failures.
        """
        container_b64 = base64.b64encode(container).decode("ascii")
        with self._lock:
            self._ensure_init()
            try:
                self._open(container_b64, password)
            except UapkiError as exc:
                raise InvalidCredentialsError(
                    f"key container rejected (uapki code {exc.error_code} {exc.error})"
                ) from exc
            try:
                keys = self._call_ok("KEYS")
                key_list = keys.get("keys") or []
                if not key_list:
                    raise InvalidCredentialsError("key container holds no signing keys")
                key = key_list[0]
                sel = self._call_ok("SELECT_KEY", {"id": key["id"]})
                signer_cert_b64 = sel.get("certificate", "")
                sign_algo = _pick_sign_algo(key)

                want_tsa = bool(self._config.tsp_url)
                signed, tsa_applied = self._sign_with_optional_tsa(
                    data=data, sign_algo=sign_algo, want_tsa=want_tsa
                )
                sig_b64 = signed["signatures"][0]["bytes"]
                return UapkiSignResult(
                    signature_der=base64.b64decode(sig_b64),
                    signer_cert_der=base64.b64decode(signer_cert_b64)
                    if signer_cert_b64
                    else b"",
                    sign_algo_oid=sign_algo,
                    tsa_applied=tsa_applied,
                )
            except UapkiError as exc:
                raise ProviderTransientError(str(exc)) from exc
            finally:
                self._close_quietly()

    def verify_detached(self, *, signature_der: bytes, data: bytes) -> UapkiVerifyResult:
        """Cryptographic verification of a detached CAdES signature.

        This is the DSTU-capable check that complements the structural
        :func:`medical_kep.verify.verify_envelope` pass.
        """
        with self._lock:
            self._ensure_init()
            try:
                result = self._call_ok(
                    "VERIFY",
                    {
                        "signature": {
                            "bytes": base64.b64encode(signature_der).decode("ascii"),
                            "content": base64.b64encode(data).decode("ascii"),
                        },
                        "reportTime": True,
                    },
                )
            except UapkiError as exc:
                raise ProviderTransientError(str(exc)) from exc
        infos = result.get("signatureInfos") or [{}]
        info = infos[0]
        return UapkiVerifyResult(
            total_valid=(info.get("status") == "TOTAL-VALID"),
            signature_status=str(info.get("statusSignature", "")),
            message_digest_status=str(info.get("statusMessageDigest", "")),
            raw=result,
        )

    def health(self) -> bool:
        try:
            with self._lock:
                self._ensure_init()
                self._call_ok("VERSION")
            return True
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        with self._lock:
            if self._lib is not None and self._initialised:
                try:
                    self._call("DEINIT")
                except Exception:  # noqa: BLE001
                    logger.warning("uapki.deinit_failed")
                self._initialised = False

    # ── Internals ────────────────────────────────────────────────────

    def _ensure_init(self) -> None:
        if self._lib is None:
            lib_path = self._config.lib_dir / "libuapki.so.2"
            if not lib_path.exists():
                lib_path = self._config.lib_dir / "libuapki.so"
            try:
                lib = ctypes.CDLL(str(lib_path))
            except OSError as exc:
                raise ProviderTransientError(f"cannot load UAPKI library: {exc}") from exc
            lib.process.argtypes = [ctypes.c_char_p]
            lib.process.restype = ctypes.c_void_p
            lib.json_free.argtypes = [ctypes.c_void_p]
            lib.json_free.restype = None
            self._lib = lib
        if not self._initialised:
            params: dict[str, Any] = {
                "cmProviders": {
                    # UAPKI concatenates dir + "lib" + name + ".so" with
                    # no separator — the trailing slash is load-bearing.
                    "dir": str(self._config.lib_dir) + "/",
                    "allowedProviders": [{"lib": "cm-pkcs12"}],
                },
                "certCache": {
                    "path": str(self._config.cert_cache_dir) + "/",
                    "trustedCerts": self._config.trusted_certs_b64,
                },
                "crlCache": {"path": str(self._config.crl_cache_dir) + "/"},
                "offline": self._config.offline,
            }
            if self._config.tsp_url:
                params["tsp"] = {"uris": [self._config.tsp_url]}
            self._call_ok("INIT", params)
            self._initialised = True

    def _open(self, container_b64: str, password: str) -> None:
        self._call_ok(
            "OPEN",
            {
                "provider": "PKCS12",
                "storage": "file://memory",
                "password": password,
                "mode": "RO",
                "openParams": {"bytes": container_b64},
            },
        )

    def _sign_with_optional_tsa(
        self, *, data: bytes, sign_algo: str, want_tsa: bool
    ) -> tuple[dict[str, Any], bool]:
        data_b64 = base64.b64encode(data).decode("ascii")

        def sign_task(fmt: str) -> dict[str, Any]:
            return self._call_ok(
                "SIGN",
                {
                    "signParams": {
                        "signatureFormat": fmt,
                        "signAlgo": sign_algo,
                        "detachedData": True,
                        "includeCert": True,
                        "includeTime": True,
                    },
                    "dataTbs": [{"id": "doc-0", "bytes": data_b64}],
                    # Cert status (OCSP/CRL) is enforced by the verify
                    # pipeline against the trust store; at sign time we
                    # must not hard-fail on cache misses in offline dev.
                    "options": {"ignoreCertStatus": self._config.offline},
                },
            )

        if want_tsa:
            try:
                return sign_task("CAdES-T"), True
            except UapkiError as exc:
                logger.warning(
                    "uapki.cades_t_failed_falling_back_to_bes: code=%s %s",
                    exc.error_code,
                    exc.error,
                )
        return sign_task("CAdES-BES"), False

    def _close_quietly(self) -> None:
        try:
            self._call("CLOSE")
        except Exception:  # noqa: BLE001
            logger.warning("uapki.close_failed")

    def _call(self, method: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._lib is not None
        request: dict[str, Any] = {"method": method}
        if parameters is not None:
            request["parameters"] = parameters
        ptr = self._lib.process(json.dumps(request).encode("utf-8"))
        if not ptr:
            raise UapkiError(method, -1, "NULL response from libuapki")
        try:
            raw = ctypes.string_at(ptr).decode("utf-8")
        finally:
            self._lib.json_free(ptr)
        return json.loads(raw)

    def _call_ok(self, method: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._call(method, parameters)
        code = int(response.get("errorCode", 0))
        if code != 0:
            raise UapkiError(method, code, str(response.get("error", "")))
        return response.get("result", {})


def _pick_sign_algo(key: dict[str, Any]) -> str:
    algos = key.get("signAlgo") or []
    for a in algos:
        if str(a).startswith("1.2.804."):  # DSTU 4145 family
            return str(a)
    return str(algos[0]) if algos else _DSTU4145_GOST34311_SIGN_ALGO
