"""clamd INSTREAM scan over TCP. Dev may run with scanning disabled
(startup WARNING, rule BE7); when enabled, failures fail CLOSED — an
unscannable file is not ingested."""

from __future__ import annotations

import asyncio
import struct


class VirusScanError(Exception):
    """Scanner unreachable / protocol error — treat the file as unscanned."""


class VirusFoundError(Exception):
    def __init__(self, signature: str) -> None:
        super().__init__(f"virus detected: {signature}")
        self.signature = signature


class ClamAvScanner:
    _CHUNK = 64 * 1024

    def __init__(self, *, host: str, port: int, timeout_s: float = 60.0) -> None:
        self._host = host
        self._port = port
        self._timeout_s = timeout_s

    async def scan(self, content: bytes) -> None:
        """Raises VirusFoundError on detection, VirusScanError on failure."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), timeout=self._timeout_s
            )
            try:
                writer.write(b"zINSTREAM\0")
                for offset in range(0, len(content), self._CHUNK):
                    block = content[offset : offset + self._CHUNK]
                    writer.write(struct.pack("!I", len(block)) + block)
                writer.write(struct.pack("!I", 0))
                await writer.drain()
                reply = await asyncio.wait_for(reader.read(512), timeout=self._timeout_s)
            finally:
                writer.close()
        except (OSError, TimeoutError) as exc:
            raise VirusScanError(f"clamd unreachable: {exc}") from exc
        text = reply.decode("utf-8", errors="replace").strip("\0").strip()
        if text.endswith("OK"):
            return
        if "FOUND" in text:
            raise VirusFoundError(text)
        raise VirusScanError(f"unexpected clamd reply: {text!r}")
