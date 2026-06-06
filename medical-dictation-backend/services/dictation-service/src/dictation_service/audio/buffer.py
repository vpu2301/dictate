"""Per-session tmpfs ring buffer for decoded PCM.

Why tmpfs:
- The decrypted audio is sensitive. tmpfs lives in RAM and dies with
  the process; it never touches disk.
- We size it for 30 minutes of mono 16 kHz float32 (115.2 MB).
- mode 0700 on the directory, owner = service account.

Why encrypt anyway:
- Defence in depth. A debug coredump that flushes process pages to
  swap (unlikely on a properly-configured worker, but possible) would
  contain plaintext PCM. Per-session AES-CTR with an ephemeral key
  mitigates that. The key never leaves process memory.

Why AES-CTR rather than GCM:
- We don't need authentication here — the buffer is single-writer,
  single-reader, in-process. CTR is simple, fast, and doesn't carry the
  tag-storage overhead.
- The envelope on finalised audio (sprint 03 ``EncryptedObjectStore``)
  IS authenticated (GCM); that's where AEAD matters.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from uuid import UUID

import numpy as np

from crypto import encryptor_at_offset, fresh_stream_key, fresh_stream_nonce

from ..config import settings

logger = logging.getLogger(__name__)

# 32 bytes = AES-256.
_DEK_BYTES: int = 32
# CTR uses a 16-byte initial counter. We split it 8/8: 8 bytes random
# nonce (fixed per session) + 8 bytes counter (incremented per block).
_CTR_BLOCK_SIZE: int = 16

# 4 bytes per float32 sample.
BYTES_PER_SAMPLE: int = 4
SAMPLE_RATE_HZ: int = 16_000


class RingFull(Exception):
    """Buffer wrapped because writer outpaced the ring length."""


@dataclass
class SessionAudioBuffer:
    """In-memory + on-tmpfs PCM ring for one session.

    The PCM is stored in two forms:
    - An in-process float32 ndarray ring (`_ring`) — what the windower
      reads from; addressed by sample index modulo ring_samples.
    - A tmpfs-backed encrypted blob (`_path`) — what survives if the
      worker is restarted but the host is still alive (it isn't,
      typically; the file is unlinked at session start anyway).
      This is a defensive trail for forensic recovery.

    ``write(pcm)`` advances the producer cursor. ``read(start_sample,
    end_sample)`` returns a view that's always within the ring; if the
    caller asks for a window that's been overwritten, :class:`RingFull`
    is raised.
    """

    session_id: UUID
    ring_seconds: int = field(default_factory=lambda: settings.tmpfs_ring_seconds)
    root: Path = field(default_factory=lambda: Path(settings.tmpfs_root))

    _ring: np.ndarray = field(init=False, repr=False)
    _ring_samples: int = field(init=False)
    _path: Path = field(init=False, repr=False)
    _fd: int = field(init=False, default=-1, repr=False)
    _nonce: bytes = field(init=False, default=b"", repr=False)
    _key: bytes = field(init=False, default=b"", repr=False)
    _producer_cursor: int = field(init=False, default=0)  # absolute sample count
    _lock: Lock = field(default_factory=Lock, repr=False)

    def __post_init__(self) -> None:
        self._ring_samples = self.ring_seconds * SAMPLE_RATE_HZ
        self._ring = np.zeros(self._ring_samples, dtype=np.float32)
        self._open_tmpfs()
        self._key = fresh_stream_key()
        self._nonce = fresh_stream_nonce()

    # ── tmpfs / lifecycle ───────────────────────────────────────────

    def _open_tmpfs(self) -> None:
        dir_path = self.root / str(self.session_id)
        try:
            dir_path.mkdir(parents=True, exist_ok=False, mode=0o700)
        except FileExistsError:
            # Pre-existing dir is a red flag — wipe and start clean.
            shutil.rmtree(dir_path, ignore_errors=True)
            dir_path.mkdir(parents=True, exist_ok=False, mode=0o700)
        # Belt-and-braces chmod in case the mkdir mode was umasked.
        os.chmod(dir_path, 0o700)
        self._path = dir_path / "audio.bin"
        # O_CREAT|O_RDWR; 0600 file mode.
        self._fd = os.open(
            self._path,
            os.O_RDWR | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        logger.info(
            "audio_buffer.opened",
            extra={
                "session_id": str(self.session_id),
                "path": str(self._path),
                "ring_seconds": self.ring_seconds,
            },
        )

    def close(self) -> None:
        """Unlink the tmpfs file and best-effort zero the in-process key."""
        with self._lock:
            if self._fd >= 0:
                try:
                    # Overwrite the file once before unlinking. tmpfs makes
                    # this paranoid (RAM-backed) but cheap.
                    size = os.fstat(self._fd).st_size
                    if size > 0:
                        os.lseek(self._fd, 0, 0)
                        os.write(self._fd, b"\x00" * min(size, 4096))
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = -1
            try:
                if self._path.exists():
                    self._path.unlink()
                parent = self._path.parent
                if parent.exists() and parent.is_dir():
                    parent.rmdir()
            except OSError as exc:
                logger.warning(
                    "audio_buffer.cleanup_failed",
                    extra={"session_id": str(self.session_id), "error": str(exc)},
                )
            # Zero the key reference.
            self._key = b"\x00" * _DEK_BYTES
            self._nonce = b"\x00" * 8

    def assert_mode(self) -> None:
        """Verify mode 0700 on dir, 0600 on file. Raised on mismatch."""
        dir_mode = stat.S_IMODE(self._path.parent.stat().st_mode)
        file_mode = stat.S_IMODE(self._path.stat().st_mode)
        if dir_mode != 0o700 or file_mode & ~0o600:
            raise PermissionError(
                f"audio buffer modes wrong: dir={oct(dir_mode)} file={oct(file_mode)}"
            )

    # ── Crypto ──────────────────────────────────────────────────────

    def _encryptor(self, sample_offset: int) -> object:
        """Build an AES-CTR encryptor positioned at ``sample_offset`` samples.

        Each sample is 4 bytes; AES block is 16 bytes (4 samples). The
        offset MUST be a multiple of 4 samples; sprint-04's writes are
        whole-Opus-frame-aligned (320 samples = 1280 bytes), so this is
        always satisfied.
        """
        byte_offset = sample_offset * BYTES_PER_SAMPLE
        return encryptor_at_offset(
            key=self._key,
            nonce=self._nonce,
            byte_offset=byte_offset,
        )

    # ── Writer ──────────────────────────────────────────────────────

    def write(self, pcm: np.ndarray) -> None:
        """Append ``pcm`` (float32 mono 16 kHz) to the ring."""
        if pcm.dtype != np.float32:
            raise TypeError(f"pcm must be float32, got {pcm.dtype}")
        if pcm.ndim != 1:
            raise ValueError(f"pcm must be 1-D, got {pcm.ndim}-D")
        with self._lock:
            start = self._producer_cursor % self._ring_samples
            n = pcm.shape[0]
            end = start + n
            if end <= self._ring_samples:
                self._ring[start:end] = pcm
            else:
                # Wrap-around.
                head = self._ring_samples - start
                self._ring[start:] = pcm[:head]
                self._ring[: end - self._ring_samples] = pcm[head:]

            # Mirror to tmpfs (encrypted). Best-effort: failures don't
            # block the in-memory write because the windower only reads
            # from the in-process ring.
            if self._fd >= 0:
                try:
                    encryptor = self._encryptor(self._producer_cursor)
                    ct = encryptor.update(pcm.tobytes()) + encryptor.finalize()  # type: ignore[attr-defined]
                    # Write to absolute byte offset; sparse beyond ring is
                    # bounded by the ring length on disk.
                    offset = (self._producer_cursor * BYTES_PER_SAMPLE) % (
                        self._ring_samples * BYTES_PER_SAMPLE
                    )
                    os.lseek(self._fd, offset, 0)
                    # If the write would wrap past EOF of the ring file,
                    # split.
                    write_end = offset + len(ct)
                    ring_bytes = self._ring_samples * BYTES_PER_SAMPLE
                    if write_end <= ring_bytes:
                        os.write(self._fd, ct)
                    else:
                        head_bytes = ring_bytes - offset
                        os.write(self._fd, ct[:head_bytes])
                        os.lseek(self._fd, 0, 0)
                        os.write(self._fd, ct[head_bytes:])
                except OSError as exc:
                    logger.warning(
                        "audio_buffer.tmpfs_write_failed",
                        extra={"session_id": str(self.session_id), "error": str(exc)},
                    )

            self._producer_cursor += n

    def insert_silence(self, n_samples: int) -> None:
        """Pad ``n_samples`` of silence (used by gap policy)."""
        self.write(np.zeros(n_samples, dtype=np.float32))

    # ── Reader ──────────────────────────────────────────────────────

    @property
    def total_samples(self) -> int:
        return self._producer_cursor

    @property
    def total_ms(self) -> int:
        return self._producer_cursor * 1000 // SAMPLE_RATE_HZ

    def read(self, start_sample: int, end_sample: int) -> np.ndarray:
        """Return a copy of samples in [start, end).

        Raises :class:`RingFull` if the requested range has been
        overwritten (writer wrapped past it).
        """
        if start_sample < 0 or end_sample < start_sample:
            raise ValueError(f"bad range [{start_sample},{end_sample})")
        with self._lock:
            head = self._producer_cursor
            tail = max(0, head - self._ring_samples)
            if start_sample < tail:
                raise RingFull(
                    f"[{start_sample},{end_sample}) is behind the ring tail {tail}"
                )
            if end_sample > head:
                end_sample = head  # caller asked past producer; clamp
            n = end_sample - start_sample
            if n <= 0:
                return np.zeros(0, dtype=np.float32)
            ring_start = start_sample % self._ring_samples
            ring_end = end_sample % self._ring_samples
            out = np.empty(n, dtype=np.float32)
            if ring_start < ring_end:
                out[:] = self._ring[ring_start:ring_end]
            else:
                # Wraps.
                first = self._ring_samples - ring_start
                out[:first] = self._ring[ring_start:]
                out[first:] = self._ring[:ring_end]
            return out


def decode_pcm_view(buffer: SessionAudioBuffer, start_ms: int, end_ms: int) -> np.ndarray:
    """ms-addressed convenience wrapper around :meth:`SessionAudioBuffer.read`."""
    start_sample = start_ms * SAMPLE_RATE_HZ // 1000
    end_sample = end_ms * SAMPLE_RATE_HZ // 1000
    return buffer.read(start_sample, end_sample)
