"""Master-key providers.

A master-key provider knows how to ``wrap`` (encrypt) a plaintext tenant KEK
into ciphertext suitable for at-rest storage, and ``unwrap`` it back. The
sprint-03 production stack ships ``FileMasterKeyProvider``; sprint 16 swaps
in ``KmsMasterKeyProvider`` for AWS KMS / Hashicorp Vault.

Why the indirection? The wrapping mechanism changes across environments,
but the envelope contract (per-object DEK, per-tenant KEK, master KEK)
must not change. Pinning the Protocol now means sprint 16's KMS migration
is a 1-line swap in the service composition, plus the re-wrap procedure.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .exceptions import DecryptError, MasterKeyError, MasterKeyPermissionError

logger = logging.getLogger(__name__)

# 32 bytes = AES-256-GCM key.
MASTER_KEY_SIZE_BYTES: int = 32
GCM_IV_SIZE_BYTES: int = 12
GCM_TAG_SIZE_BYTES: int = 16

# Deterministic associated data for KEK-wrapping. Pinning this to a
# version-tagged byte string means future format changes don't silently
# decrypt against the old format — they fail closed.
MASTER_WRAP_AAD: bytes = b"mdx-master-kek-v1"

# Marker the FileMasterKeyProvider returns. The KMS provider returns
# ``vault:{mount}:{key_name}``. Stored in EnvelopeBlob so re-wrap
# migrations know which master a tenant KEK was wrapped under.
FILE_MASTER_KEY_ID: str = "file-v1"
VAULT_MASTER_KEY_ID_PREFIX: str = "vault:"


def _reveal(token: object) -> str:
    """Unwrap ``Secret[str]`` (whose ``.value()`` is a method) or pass a str."""
    accessor = getattr(token, "value", None)
    value = accessor() if callable(accessor) else token
    return value if isinstance(value, str) else ""


class MasterKeyProvider(Protocol):
    """Wrap / unwrap a tenant KEK under the environment's master key.

    Implementations MUST be safe to call from multiple coroutines
    concurrently. They MUST NOT expose plaintext master-key bytes via
    their public API.
    """

    async def wrap(self, kek_plaintext: bytes) -> tuple[str, bytes]:
        """Wrap a 32-byte tenant KEK.

        Returns ``(master_key_id, wrapped_kek)``. ``wrapped_kek`` is
        ``iv || ciphertext || tag`` (12 + N + 16 bytes for AES-GCM).
        """
        ...

    async def unwrap(self, master_key_id: str, wrapped_kek: bytes) -> bytes:
        """Unwrap to plaintext tenant KEK. Caller must zero the result
        when finished. Raises :class:`DecryptError` on tag mismatch."""
        ...


class FileMasterKeyProvider:
    """Read the master key from a 0400-mode file on disk.

    Production swaps this for :class:`KmsMasterKeyProvider`. The file path
    is configurable via ``MDX_MASTER_KEY_PATH``; in dev compose it's
    bind-mounted from ``infra/dev/master.key``.

    Startup self-check: refuses to operate if the file mode is more
    permissive than 0400, if the file is missing, or if it's the wrong
    length. The check happens in :meth:`startup_self_check`, which the
    service lifespan calls before any traffic is accepted.
    """

    def __init__(self, *, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._aead: AESGCM | None = None  # lazily loaded; cleared on rotate

    @property
    def master_key_id(self) -> str:
        return FILE_MASTER_KEY_ID

    def handles(self, master_key_id: str) -> bool:
        return master_key_id == FILE_MASTER_KEY_ID

    async def startup_self_check(self) -> None:
        """Verify the master key file's existence, mode, and length.

        Called from each service's lifespan ``startup``. Raises a precise
        :class:`MasterKeyError` subclass that points to the runbook so an
        operator can act without paging the on-call engineer.
        """
        if not self._path.exists():
            raise MasterKeyError(
                f"master key file not found at {self._path!s}. "
                "See docs/runbooks/asr-worker.md § master-key-missing."
            )
        try:
            st = self._path.stat()
        except OSError as exc:
            raise MasterKeyError(
                f"master key file at {self._path!s} cannot be stat()'d: {type(exc).__name__}"
            ) from exc

        # Accept any mode whose permission bits are a SUBSET of 0400.
        # That is: no group/other access, and no write-by-owner.
        mode_bits = stat.S_IMODE(st.st_mode)
        if mode_bits & ~0o400:
            raise MasterKeyPermissionError(
                f"master key file at {self._path!s} has mode {oct(mode_bits)}; "
                "must be 0400 (read-only by owner). See "
                "docs/runbooks/asr-worker.md § master-key-permissions."
            )

        if st.st_size != MASTER_KEY_SIZE_BYTES:
            raise MasterKeyError(
                f"master key file at {self._path!s} is {st.st_size} bytes; "
                f"expected exactly {MASTER_KEY_SIZE_BYTES} bytes for AES-256."
            )

        # Load once and keep AESGCM around for the process lifetime.
        # We deliberately do NOT hold the raw 32-byte key in a Python str;
        # the AESGCM instance owns the bytes internally.
        with self._path.open("rb") as f:
            raw = f.read(MASTER_KEY_SIZE_BYTES)
        try:
            self._aead = AESGCM(raw)
        finally:
            # Best-effort zero — Python doesn't guarantee no copies, but
            # we at least overwrite our local reference.
            raw = b"\x00" * MASTER_KEY_SIZE_BYTES

        logger.info(
            "master_key.loaded",
            extra={
                "master_key_id": self.master_key_id,
                "path": str(self._path),
                "mode": oct(mode_bits),
            },
        )

    def _aead_or_raise(self) -> AESGCM:
        if self._aead is None:
            raise MasterKeyError("master key not loaded. Call startup_self_check() before use.")
        return self._aead

    async def wrap(self, kek_plaintext: bytes) -> tuple[str, bytes]:
        if len(kek_plaintext) != 32:
            raise MasterKeyError(f"tenant KEK must be 32 bytes, got {len(kek_plaintext)}")
        iv = os.urandom(GCM_IV_SIZE_BYTES)
        ct = self._aead_or_raise().encrypt(iv, kek_plaintext, MASTER_WRAP_AAD)
        # cryptography's AESGCM returns ciphertext || tag concatenated;
        # we prepend the IV so the on-disk format is iv || ct || tag.
        return FILE_MASTER_KEY_ID, iv + ct

    async def unwrap(self, master_key_id: str, wrapped_kek: bytes) -> bytes:
        if master_key_id != FILE_MASTER_KEY_ID:
            raise MasterKeyError(
                f"master_key_id {master_key_id!r} is not handled by "
                "FileMasterKeyProvider. Wire build_master_key_provider / "
                "CompositeMasterKeyProvider for mixed-master reads (ADR-0011)."
            )
        if len(wrapped_kek) < GCM_IV_SIZE_BYTES + GCM_TAG_SIZE_BYTES:
            raise DecryptError("wrapped KEK is too short to be valid")
        iv, ct = wrapped_kek[:GCM_IV_SIZE_BYTES], wrapped_kek[GCM_IV_SIZE_BYTES:]
        try:
            return self._aead_or_raise().decrypt(iv, ct, MASTER_WRAP_AAD)
        except InvalidTag as exc:
            raise DecryptError(
                "master-key unwrap failed: GCM tag mismatch. The wrapped KEK "
                "may have been tampered with, or the master key has rotated."
            ) from exc


class KmsMasterKeyProvider:
    """Vault-Transit-backed master key (sprint 16, ADR-0011's promised swap).

    The master KEK never leaves the KMS: ``wrap``/``unwrap`` are remote
    calls to Vault's Transit engine (``encrypt``/``decrypt``). The ≤60 s
    plaintext tenant-KEK cache in :class:`~crypto.tenant_kek.TenantKekRepository`
    keeps KMS load at one call per tenant per TTL — envelope semantics
    unchanged.

    ``master_key_id`` format: ``vault:{mount}:{key_name}``. The Vault
    ciphertext string itself carries the key *version* (``vault:vN:…``),
    so key rotation inside Transit needs no id change — Vault decrypts
    old versions transparently (per its ``min_decryption_version``).

    Vault Transit was chosen as the reference implementation because it is
    self-hostable in-jurisdiction (UA/on-prem posture, ADR-0006); the
    Protocol seam means a cloud KMS is another implementation, not a
    redesign. Fail-closed: :meth:`startup_self_check` performs a live
    encrypt/decrypt round-trip and raises :class:`MasterKeyError` if the
    KMS is unreachable or the key is absent — the service refuses to start,
    same posture as master-key-missing (sprint-03 chaos).
    """

    def __init__(
        self,
        *,
        addr: str,
        token: object,
        key_name: str,
        mount: str = "transit",
        timeout_seconds: float = 5.0,
        http_client: Any | None = None,
    ) -> None:
        import httpx

        self._addr = addr.rstrip("/")
        # Accept Secret[str] (house style) or a plain str; hold only the
        # revealed value privately — never expose it via public API/repr.
        self._token: str = _reveal(token)
        if not self._token:
            raise MasterKeyError("Vault token is empty; refusing to construct provider")
        self._mount = mount.strip("/")
        self._key_name = key_name
        self._owns_client = http_client is None
        self._client: httpx.AsyncClient = http_client or httpx.AsyncClient(
            timeout=timeout_seconds
        )
        self._checked = False

    @property
    def master_key_id(self) -> str:
        return f"{VAULT_MASTER_KEY_ID_PREFIX}{self._mount}:{self._key_name}"

    def handles(self, master_key_id: str) -> bool:
        return master_key_id == self.master_key_id

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _url(self, op: str) -> str:
        return f"{self._addr}/v1/{self._mount}/{op}/{self._key_name}"

    async def _post(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        try:
            resp = await self._client.post(
                self._url(op),
                json=payload,
                headers={"X-Vault-Token": self._token},
            )
        except httpx.HTTPError as exc:
            raise MasterKeyError(
                f"Vault Transit unreachable at {self._addr} ({type(exc).__name__}). "
                "See docs/runbooks/kms.md § vault-unreachable."
            ) from exc
        if resp.status_code != 200:
            raise MasterKeyError(
                f"Vault Transit {op} returned {resp.status_code}: "
                f"{resp.text[:200]}. See docs/runbooks/kms.md."
            )
        data = resp.json().get("data")
        if not isinstance(data, dict):
            raise MasterKeyError(f"Vault Transit {op} returned no data object")
        return data

    async def startup_self_check(self) -> None:
        """Live encrypt/decrypt round-trip. Raises MasterKeyError on any
        failure so the service lifespan refuses to start (fail-closed)."""
        import base64

        probe = os.urandom(16)
        data = await self._post(
            "encrypt", {"plaintext": base64.b64encode(probe).decode("ascii")}
        )
        ct = data.get("ciphertext")
        if not isinstance(ct, str):
            raise MasterKeyError("Vault Transit self-check: encrypt returned no ciphertext")
        back = await self._post("decrypt", {"ciphertext": ct})
        pt_b64 = back.get("plaintext")
        if not isinstance(pt_b64, str) or base64.b64decode(pt_b64) != probe:
            raise MasterKeyError(
                "Vault Transit self-check: decrypt round-trip mismatch. "
                "The transit key may be derived/convergent — use a plain "
                "aes256-gcm96 key. See docs/runbooks/kms.md."
            )
        self._checked = True
        logger.info(
            "master_key.kms_ready",
            extra={"master_key_id": self.master_key_id, "vault_addr": self._addr},
        )

    async def wrap(self, kek_plaintext: bytes) -> tuple[str, bytes]:
        import base64

        if len(kek_plaintext) != MASTER_KEY_SIZE_BYTES:
            raise MasterKeyError(
                f"tenant KEK must be {MASTER_KEY_SIZE_BYTES} bytes, got {len(kek_plaintext)}"
            )
        data = await self._post(
            "encrypt",
            {"plaintext": base64.b64encode(kek_plaintext).decode("ascii")},
        )
        ct = data.get("ciphertext")
        if not isinstance(ct, str) or not ct.startswith("vault:"):
            raise MasterKeyError("Vault Transit encrypt returned malformed ciphertext")
        # Stored as the UTF-8 bytes of Vault's versioned ciphertext string.
        return self.master_key_id, ct.encode("utf-8")

    async def unwrap(self, master_key_id: str, wrapped_kek: bytes) -> bytes:
        import base64

        if not self.handles(master_key_id):
            raise MasterKeyError(
                f"master_key_id {master_key_id!r} is not handled by this "
                f"KmsMasterKeyProvider ({self.master_key_id}). Wire a "
                "CompositeMasterKeyProvider for mixed-master reads (ADR-0011)."
            )
        try:
            ct = wrapped_kek.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DecryptError("wrapped KEK is not a Vault ciphertext string") from exc
        data = await self._post("decrypt", {"ciphertext": ct})
        pt_b64 = data.get("plaintext")
        if not isinstance(pt_b64, str):
            raise DecryptError("Vault Transit decrypt returned no plaintext")
        plaintext = base64.b64decode(pt_b64)
        if len(plaintext) != MASTER_KEY_SIZE_BYTES:
            raise DecryptError(
                f"Vault-unwrapped KEK is {len(plaintext)} bytes; expected "
                f"{MASTER_KEY_SIZE_BYTES}. The wrapped row is corrupt."
            )
        return plaintext


class CompositeMasterKeyProvider:
    """Route ``unwrap`` by ``master_key_id``; ``wrap`` under the primary.

    Exists for the KMS migration window (ADR-0011 re-wrap procedure):
    while ``scripts/kms/rewrap-tenant-keks.py`` is moving rows from
    ``file-v1`` to ``vault:…``, both masters are live and every row must
    keep decrypting. After the re-wrap completes the file member can be
    dropped from configuration.
    """

    def __init__(self, *, primary: Any, fallbacks: tuple[Any, ...] = ()) -> None:
        self._primary = primary
        self._members: tuple[Any, ...] = (primary, *fallbacks)

    @property
    def master_key_id(self) -> str:
        mid: str = self._primary.master_key_id
        return mid

    @property
    def members(self) -> tuple[Any, ...]:
        return self._members

    def _handles(self, member: Any, master_key_id: str) -> bool:
        handles = getattr(member, "handles", None)
        if callable(handles):
            return bool(handles(master_key_id))
        return bool(getattr(member, "master_key_id", None) == master_key_id)

    async def startup_self_check(self) -> None:
        """Fail-closed on the PRIMARY; fallback members that fail their
        check are logged loudly but tolerated — a fallback exists only to
        read not-yet-re-wrapped rows, and those reads will fail precisely
        and audibly at unwrap time if the member is genuinely broken."""
        check = getattr(self._primary, "startup_self_check", None)
        if callable(check):
            await check()
        for member in self._members[1:]:
            mcheck = getattr(member, "startup_self_check", None)
            if not callable(mcheck):
                continue
            try:
                await mcheck()
            except MasterKeyError as exc:
                logger.warning(
                    "master_key.fallback_unavailable",
                    extra={
                        "master_key_id": getattr(member, "master_key_id", "?"),
                        "error": str(exc),
                    },
                )

    async def wrap(self, kek_plaintext: bytes) -> tuple[str, bytes]:
        result: tuple[str, bytes] = await self._primary.wrap(kek_plaintext)
        return result

    async def unwrap(self, master_key_id: str, wrapped_kek: bytes) -> bytes:
        for member in self._members:
            if self._handles(member, master_key_id):
                plaintext: bytes = await member.unwrap(master_key_id, wrapped_kek)
                return plaintext
        raise MasterKeyError(
            f"no configured master-key provider handles {master_key_id!r}. "
            "If this row predates the KMS migration, re-add the file "
            "provider (MDX_MASTER_KEY_PATH) as a fallback and finish the "
            "re-wrap (scripts/kms/rewrap-tenant-keks.py, ADR-0011)."
        )

    async def aclose(self) -> None:
        for member in self._members:
            close = getattr(member, "aclose", None)
            if callable(close):
                await close()


def build_master_key_provider(
    *,
    provider: str,
    file_path: str | os.PathLike[str] | None = None,
    vault_addr: str | None = None,
    vault_token: object | None = None,
    vault_transit_key: str = "mdx-master",
    vault_transit_mount: str = "transit",
) -> Any:
    """The single composition helper every service calls (the '1-line swap').

    ``provider='file'``  → :class:`FileMasterKeyProvider` (dev default,
    behaviour identical to pre-sprint-16).
    ``provider='vault'`` → :class:`KmsMasterKeyProvider` as primary, with
    the file provider included as a read-only fallback **iff** the file
    exists on disk — so the migration window keeps every old row
    decryptable, and a finished migration (key file removed) runs pure-KMS.

    The returned object satisfies :class:`MasterKeyProvider` and exposes
    ``startup_self_check`` — call it in the lifespan, exactly as before.
    """
    provider = provider.strip().lower()
    if provider == "file":
        if not file_path:
            raise MasterKeyError("MDX_MASTER_KEY_PROVIDER=file requires MDX_MASTER_KEY_PATH")
        return FileMasterKeyProvider(path=file_path)
    if provider == "vault":
        if not vault_addr:
            raise MasterKeyError("MDX_MASTER_KEY_PROVIDER=vault requires MDX_VAULT_ADDR")
        kms = KmsMasterKeyProvider(
            addr=vault_addr,
            token=vault_token,
            key_name=vault_transit_key,
            mount=vault_transit_mount,
        )
        fallbacks: tuple[Any, ...] = ()
        if file_path and Path(file_path).exists():
            fallbacks = (FileMasterKeyProvider(path=file_path),)
            logger.info(
                "master_key.file_fallback_active",
                extra={"path": str(file_path), "reason": "kms-migration-window"},
            )
        return CompositeMasterKeyProvider(primary=kms, fallbacks=fallbacks)
    raise MasterKeyError(
        f"unknown MDX_MASTER_KEY_PROVIDER {provider!r}; expected 'file' or 'vault'"
    )
