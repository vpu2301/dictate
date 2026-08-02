"""Dedicated inline concurrency slots.

A small semaphore pool (default 2) bounds how many inline completions
can hit the model at once. Combined with the caller's whole-request
``asyncio.timeout`` (slot wait INCLUDED in the budget), a long-running
generation can never starve the typing path beyond the deadline — the
request simply times out into a silent 204.

Deliberately not the dictation-service ``InferenceQueue``: that class
serialises ALL calls onto one GPU engine and tracks deadline misses per
job; here the backend server (llama-server ``--parallel N`` / Ollama
``OLLAMA_NUM_PARALLEL``) already multiplexes, and the only guarantee the
service needs is "never queue more than N inline calls".
"""

from __future__ import annotations

import asyncio
from types import TracebackType


class SlotPool:
    def __init__(self, slots: int) -> None:
        self._sem = asyncio.Semaphore(slots)

    async def __aenter__(self) -> SlotPool:
        await self._sem.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._sem.release()
