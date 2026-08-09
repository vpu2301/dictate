"""Session-revocation denylist: push/check semantics, TTL, fail-open."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from auth import Claims, RedisSessionDenylist, build_current_user, build_session_denylist


class FakePipeline:
    def __init__(self, store: FakeRedis) -> None:
        self._store = store
        self._ops: list[str] = []

    def exists(self, key: str) -> None:
        self._ops.append(key)

    async def execute(self) -> list[int]:
        if self._store.broken:
            raise ConnectionError("redis down")
        return [1 if k in self._store.data else 0 for k in self._ops]


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, tuple[bytes, int]] = {}
        self.broken = False

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    async def set(self, key: str, value: bytes, ex: int) -> None:
        if self.broken:
            raise ConnectionError("redis down")
        self.data[key] = (value, ex)

    async def delete(self, key: str) -> None:
        if self.broken:
            raise ConnectionError("redis down")
        self.data.pop(key, None)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def denylist() -> RedisSessionDenylist:
    return RedisSessionDenylist(redis=FakeRedis())


async def test_revoked_sid_is_denied(denylist: RedisSessionDenylist) -> None:
    await denylist.revoke_sid("sid-1", ttl_seconds=600)
    assert await denylist.is_revoked(sid="sid-1", sub="u1")
    assert not await denylist.is_revoked(sid="sid-2", sub="u1")


async def test_revoked_sub_denies_every_session(denylist: RedisSessionDenylist) -> None:
    await denylist.revoke_sub("u1", ttl_seconds=900)
    assert await denylist.is_revoked(sid="any-sid", sub="u1")
    await denylist.clear_sub("u1")
    assert not await denylist.is_revoked(sid="any-sid", sub="u1")


async def test_ttl_is_floored_never_unbounded(denylist: RedisSessionDenylist) -> None:
    await denylist.revoke_sid("sid-1", ttl_seconds=0)
    store: FakeRedis = denylist._redis  # type: ignore[assignment]
    _, ttl = store.data["mdx:revoked:sid:sid-1"]
    assert 0 < ttl <= 60  # floored, and never longer than a token lifetime


async def test_check_fails_open_when_redis_down(
    denylist: RedisSessionDenylist, caplog: pytest.LogCaptureFixture
) -> None:
    await denylist.revoke_sid("sid-1", ttl_seconds=600)
    store: FakeRedis = denylist._redis  # type: ignore[assignment]
    store.broken = True
    with caplog.at_level("WARNING"):
        assert not await denylist.is_revoked(sid="sid-1", sub="u1")
    assert any("fail_open" in r.message for r in caplog.records)


def test_builder_returns_none_when_disabled() -> None:
    assert build_session_denylist(enabled=False, redis_url="redis://x") is None


# ── build_current_user integration ─────────────────────────────────────


class _Req:
    class _State:
        pass

    def __init__(self) -> None:
        self.state = self._State()


def _claims(sid: str = "sid-1") -> Claims:
    return Claims(
        sub=uuid4(),
        tid=uuid4(),
        roles=["clinician"],
        sid=sid,
        iss="https://issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1,
    )


async def test_current_user_rejects_revoked_session(
    monkeypatch: pytest.MonkeyPatch, denylist: RedisSessionDenylist
) -> None:
    from fastapi import HTTPException

    from auth import dependencies as deps_mod

    claims = _claims()

    async def _fake_verify(token: str, **kwargs: Any) -> Claims:
        return claims

    monkeypatch.setattr(deps_mod, "verify_token", _fake_verify)
    dep = build_current_user(
        jwks_cache=object(),  # type: ignore[arg-type]
        expected_audience="mdx",
        expected_issuer="https://issuer",
        denylist=denylist,
    )

    # Not revoked → passes.
    assert await dep(_Req(), "Bearer tok") == claims

    # Revoked → 401.
    await denylist.revoke_sid(claims.sid, ttl_seconds=600)
    with pytest.raises(HTTPException) as exc:
        await dep(_Req(), "Bearer tok")
    assert exc.value.status_code == 401
    assert "revoked" in exc.value.detail.lower()

    # Redis down → fail-open: the token verifies again.
    store: FakeRedis = denylist._redis  # type: ignore[assignment]
    store.broken = True
    assert await dep(_Req(), "Bearer tok") == claims
