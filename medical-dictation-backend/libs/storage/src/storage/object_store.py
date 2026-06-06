"""The single sanctioned write/read path for tenant-bearing object data.

Wire format
-----------

Each object is laid out as::

    [4-byte big-endian header length][JSON header][raw ciphertext]

The JSON header carries every envelope-metadata field EXCEPT the
ciphertext itself; the body is the raw ciphertext (no second base64
hop — bytes are bytes). The 4-byte prefix lets readers locate the
ciphertext without buffering the whole object.

Why not store all metadata only in Postgres? Two reasons:

1. Defence in depth: a future re-key/migration script can verify that
   the stored header matches what the DB row claims about the object.
2. The reverse: the object alone is self-describing enough to attempt a
   restore if a row is lost.

The header also stores the ``master_key_id`` so when sprint 16 lands the
KMS migration, the re-wrap script knows which provider to unwrap with.
"""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from crypto import Envelope, EnvelopeBlob, EnvelopeFormatError

from .s3_client import S3Client


class ObjectStoreDisabledError(Exception):
    """Sprint-07: raised when MD_OBJECT_STORE_DISABLED is true and a
    caller tries to write. Lets the dictation-service / asr-service
    demo paths skip the audio_files row without crashing the request.
    """

HEADER_LENGTH_PREFIX_BYTES: Final = 4
HEADER_MAGIC: Final = "mdx-env-v1"


@dataclass(frozen=True, slots=True)
class ObjectHeader:
    """Decoded form of the JSON header at the head of an encrypted object."""

    magic: str
    version: int
    algorithm: str
    tenant_id: UUID
    master_key_id: str
    iv: bytes
    tag: bytes
    wrapped_dek: bytes
    dek_iv: bytes
    dek_tag: bytes
    extra_aad: bytes | None

    def to_blob(self, ciphertext: bytes) -> EnvelopeBlob:
        return EnvelopeBlob(
            ciphertext=ciphertext,
            iv=self.iv,
            tag=self.tag,
            wrapped_dek=self.wrapped_dek,
            dek_iv=self.dek_iv,
            dek_tag=self.dek_tag,
            tenant_id=self.tenant_id,
            master_key_id=self.master_key_id,
            algorithm=self.algorithm,
            version=self.version,
            extra_aad=self.extra_aad,
        )


class EncryptedObjectStore:
    """High-level encrypted blob store.

    The contract is intentionally narrow: ``put`` and ``get`` and nothing
    that would let a caller round-trip plaintext through an unwrapped path.
    """

    def __init__(
        self,
        *,
        s3: S3Client,
        bucket: str,
        envelope: Envelope,
        disabled: bool = False,
    ) -> None:
        self._s3 = s3
        self.bucket = bucket
        self._envelope = envelope
        self._disabled = disabled

    @property
    def is_disabled(self) -> bool:
        """Sprint-07 HF Space sets ``MD_OBJECT_STORE_DISABLED=true`` to
        force a "no audio at rest" posture. The construct-from-env
        helper in services wires this; tests pass it directly.
        """
        return self._disabled

    async def put(
        self,
        *,
        key: str,
        plaintext: bytes,
        tenant_id: UUID,
        aad: bytes | None = None,
    ) -> ObjectHeader:
        """Encrypt and upload ``plaintext`` under ``key``.

        When ``disabled=True`` (sprint-07 HF Space), this raises
        :class:`ObjectStoreDisabledError`. Callers that handle the
        "no audio at rest" posture catch and route to the demo path
        (sprint-04 finalize without audio_files row).
        """
        if self._disabled:
            raise ObjectStoreDisabledError(
                "EncryptedObjectStore writes are disabled by environment "
                "(MD_OBJECT_STORE_DISABLED). Demo / privacy-first mode."
            )
        blob = await self._envelope.encrypt(plaintext, tenant_id=tenant_id, aad=aad)
        header_bytes = _encode_header(blob)
        body = (
            struct.pack(">I", len(header_bytes)) + header_bytes + blob.ciphertext
        )
        await self._s3.put_object(bucket=self.bucket, key=key, body=body)
        return _decode_header(header_bytes)

    async def get(
        self,
        *,
        key: str,
        tenant_id: UUID,
        aad: bytes | None = None,
    ) -> bytes:
        """Download and decrypt the object at ``key``.

        Refuses to attempt crypto if the on-disk tenant_id doesn't match
        the caller-supplied one (see ``Envelope.decrypt`` confused-deputy
        guard).
        """
        body = await self._s3.get_object(bucket=self.bucket, key=key)
        if len(body) < HEADER_LENGTH_PREFIX_BYTES:
            raise EnvelopeFormatError("object too short to contain a header")
        (hlen,) = struct.unpack(">I", body[:HEADER_LENGTH_PREFIX_BYTES])
        head_end = HEADER_LENGTH_PREFIX_BYTES + hlen
        if head_end > len(body):
            raise EnvelopeFormatError("declared header length exceeds object size")
        header_bytes = body[HEADER_LENGTH_PREFIX_BYTES:head_end]
        ciphertext = body[head_end:]
        header = _decode_header(header_bytes)
        blob = header.to_blob(ciphertext)
        return await self._envelope.decrypt(blob, tenant_id=tenant_id, aad=aad)

    async def presigned_url(self, *, key: str, expires_in: int) -> str:
        """Generate a short-TTL pre-signed URL.

        The URL serves **encrypted** bytes; the consumer must call
        ``get()`` (or an authenticated proxy) to obtain plaintext.

        TTL is the caller's responsibility — the orchestrator caps it.
        """
        return await self._s3.generate_presigned_url(
            bucket=self.bucket, key=key, expires_in=expires_in
        )

    async def delete(self, *, key: str) -> None:
        await self._s3.delete_object(bucket=self.bucket, key=key)


def _encode_header(blob: EnvelopeBlob) -> bytes:
    """Serialize blob metadata to the canonical JSON header form."""
    doc = {
        "magic": HEADER_MAGIC,
        "version": blob.version,
        "algorithm": blob.algorithm,
        "tenant_id": str(blob.tenant_id),
        "master_key_id": blob.master_key_id,
        "iv": base64.b64encode(blob.iv).decode("ascii"),
        "tag": base64.b64encode(blob.tag).decode("ascii"),
        "wrapped_dek": base64.b64encode(blob.wrapped_dek).decode("ascii"),
        "dek_iv": base64.b64encode(blob.dek_iv).decode("ascii"),
        "dek_tag": base64.b64encode(blob.dek_tag).decode("ascii"),
    }
    if blob.extra_aad is not None:
        doc["extra_aad"] = base64.b64encode(blob.extra_aad).decode("ascii")
    return json.dumps(doc, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _decode_header(header_bytes: bytes) -> ObjectHeader:
    try:
        doc = json.loads(header_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvelopeFormatError(f"object header is not valid JSON: {exc}") from exc

    if doc.get("magic") != HEADER_MAGIC:
        raise EnvelopeFormatError(
            f"object header magic is {doc.get('magic')!r}; expected {HEADER_MAGIC!r}"
        )

    try:
        return ObjectHeader(
            magic=doc["magic"],
            version=int(doc["version"]),
            algorithm=str(doc["algorithm"]),
            tenant_id=UUID(doc["tenant_id"]),
            master_key_id=str(doc["master_key_id"]),
            iv=base64.b64decode(doc["iv"]),
            tag=base64.b64decode(doc["tag"]),
            wrapped_dek=base64.b64decode(doc["wrapped_dek"]),
            dek_iv=base64.b64decode(doc["dek_iv"]),
            dek_tag=base64.b64decode(doc["dek_tag"]),
            extra_aad=(
                base64.b64decode(doc["extra_aad"]) if "extra_aad" in doc else None
            ),
        )
    except (KeyError, ValueError) as exc:
        raise EnvelopeFormatError(f"object header missing/invalid field: {exc}") from exc


def header_metadata_for_row(header: ObjectHeader) -> dict[str, str | int]:
    """Project ``ObjectHeader`` into a JSON-safe dict for ``audio_files.envelope_metadata``.

    Excludes only the fields that have no value at the row level (none, in
    practice — everything is wire-safe metadata). Kept as a helper so the
    persistence layer's contract with the row stays explicit.
    """
    doc: dict[str, str | int] = {
        "magic": header.magic,
        "version": header.version,
        "algorithm": header.algorithm,
        "tenant_id": str(header.tenant_id),
        "master_key_id": header.master_key_id,
        "iv": base64.b64encode(header.iv).decode("ascii"),
        "tag": base64.b64encode(header.tag).decode("ascii"),
        "wrapped_dek": base64.b64encode(header.wrapped_dek).decode("ascii"),
        "dek_iv": base64.b64encode(header.dek_iv).decode("ascii"),
        "dek_tag": base64.b64encode(header.dek_tag).decode("ascii"),
    }
    if header.extra_aad is not None:
        doc["extra_aad"] = base64.b64encode(header.extra_aad).decode("ascii")
    return doc
