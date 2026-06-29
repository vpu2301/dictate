"""Sprint-07 (ADR-0018) — the demo privacy envelope on the finalize path.

Proves that ``purge_audio`` / a disabled object store result in *no audio
at rest*: ``EncryptedObjectStore.put`` is never called and no
``audio_files`` row is written. The complementary test proves the
normal path still attempts the upload.
"""

from __future__ import annotations

import contextlib
from uuid import uuid4

import numpy as np
import pytest

from dictation_service.session import finalize as finalize_mod
from dictation_service.session.finalize import finalize_session
from dictation_service.session.manager import SessionContext


class _FakeBuffer:
    total_samples = 16_000
    _ring_samples = 16_000
    total_ms = 1_000

    def __init__(self) -> None:
        self.closed = False

    def read(self, start: int, total: int) -> np.ndarray:
        return np.full(total - start, 0.5, dtype=np.float32)

    def close(self) -> None:
        self.closed = True


class _FakeStore:
    def __init__(self, *, disabled: bool = False, put_raises: Exception | None = None):
        self._disabled = disabled
        self._put_raises = put_raises
        self.put_calls = 0
        self.bucket = "mdx-audio"

    @property
    def is_disabled(self) -> bool:
        return self._disabled

    async def put(self, **_kw):
        self.put_calls += 1
        if self._put_raises is not None:
            raise self._put_raises
        raise AssertionError("put() reached but no header fixture configured")


class _FakeAuditWriter:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    async def write_event(self, *, kind: str, **_kw) -> None:
        self.kinds.append(kind)


def _ctx() -> SessionContext:
    ctx = SessionContext(
        session_id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        language="uk",
        prompt_id=uuid4(),
        prompt_text="",
        target_kind="note",
        encounter_id=None,
        template_id=None,
    )
    ctx.buffer = _FakeBuffer()
    ctx.finalized_segments = []
    return ctx


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    """Stub the DB collaborators finalize_session reaches for the
    transcript write (and the audio-row write on the persist path)."""

    class _Conn:
        async def execute(self, *_a, **_k):
            return None

    @contextlib.asynccontextmanager
    async def _fake_tenant_connection(_pool, _tenant_id):
        yield _Conn()

    async def _fake_write_finalized(_conn, **_kw):
        return None

    monkeypatch.setattr(finalize_mod, "tenant_connection", _fake_tenant_connection)
    monkeypatch.setattr(
        finalize_mod.repository, "write_finalized", _fake_write_finalized
    )


async def test_purge_on_finalize_writes_no_audio():
    store = _FakeStore(disabled=False)
    audit = _FakeAuditWriter()
    result = await finalize_session(
        ctx=_ctx(),
        app_pool=object(),
        audit_writer=audit,
        audio_store=store,
        envelope=None,
        reason="normal",
        purge_audio=True,
    )
    assert store.put_calls == 0
    assert result.audio_file_id is None
    assert "dictation.audio.uploaded" not in audit.kinds
    assert "dictation.session.finalized" in audit.kinds


async def test_disabled_store_writes_no_audio():
    store = _FakeStore(disabled=True)
    audit = _FakeAuditWriter()
    result = await finalize_session(
        ctx=_ctx(),
        app_pool=object(),
        audit_writer=audit,
        audio_store=store,
        envelope=None,
        reason="normal",
        purge_audio=False,
    )
    assert store.put_calls == 0
    assert result.audio_file_id is None


async def test_normal_path_attempts_upload():
    # With neither flag set and a live store, the persist path must reach
    # put(); we sentinel-raise from put to prove it was called.
    store = _FakeStore(disabled=False, put_raises=RuntimeError("PUT_CALLED"))
    audit = _FakeAuditWriter()
    with pytest.raises(RuntimeError, match="PUT_CALLED"):
        await finalize_session(
            ctx=_ctx(),
            app_pool=object(),
            audit_writer=audit,
            audio_store=store,
            envelope=None,
            reason="normal",
            purge_audio=False,
        )
    assert store.put_calls == 1
