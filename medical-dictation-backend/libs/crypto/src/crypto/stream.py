"""Ephemeral seekable stream cipher.

Sprint 04's tmpfs ring buffer wants a fast, seekable, single-process,
ephemeral cipher. It is NOT a replacement for the envelope (which is
AEAD GCM for at-rest data). This primitive lives in libs/crypto so the
``check-no-direct-crypto`` CI gate stays strict — callers don't import
``cryptography.hazmat`` directly.

Properties:
- AES-256-CTR. Seekable by sample/byte offset because CTR counter
  increments deterministically per 16-byte block.
- No authentication. Single writer + single reader in the same process;
  tampering is not in the threat model for tmpfs.
- The key never leaves process memory; the helper accepts a key + nonce
  and returns an encryptor/decryptor at any block-aligned offset.

For AEAD at-rest data use :class:`Envelope`. For everything else, use
this only if you understand why AEAD isn't needed.
"""

from __future__ import annotations

from secrets import token_bytes
from typing import Final

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

CTR_BLOCK_SIZE: Final = 16
KEY_SIZE: Final = 32  # AES-256
NONCE_SIZE: Final = 8  # 8-byte fixed nonce + 8-byte counter = 16-byte counter input


def fresh_stream_key() -> bytes:
    """Return a 32-byte AES-256 key from a CSPRNG."""
    return token_bytes(KEY_SIZE)


def fresh_stream_nonce() -> bytes:
    """Return an 8-byte stream-nonce. Caller pairs it with an 8-byte counter."""
    return token_bytes(NONCE_SIZE)


def encryptor_at_offset(
    *, key: bytes, nonce: bytes, byte_offset: int
) -> object:
    """Return an AES-CTR encryptor positioned to ``byte_offset``.

    ``byte_offset`` MUST be a multiple of 16 (the AES block size); the
    counter index = byte_offset // 16. Callers in sprint-04's audio
    buffer always start at a sample boundary that's a multiple of 4
    samples = 16 bytes for float32 audio, so this is naturally aligned.
    """
    if len(key) != KEY_SIZE:
        raise ValueError(f"key must be {KEY_SIZE} bytes")
    if len(nonce) != NONCE_SIZE:
        raise ValueError(f"nonce must be {NONCE_SIZE} bytes")
    if byte_offset % CTR_BLOCK_SIZE != 0:
        raise ValueError(
            f"byte_offset {byte_offset} not aligned to {CTR_BLOCK_SIZE}-byte block"
        )
    block_index = byte_offset // CTR_BLOCK_SIZE
    counter = nonce + block_index.to_bytes(8, "big")
    return Cipher(algorithms.AES(key), modes.CTR(counter)).encryptor()
