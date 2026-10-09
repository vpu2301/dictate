"""Sprint-17 admin list surface — GET /autocomplete/phrases + /snippets.

Handlers exercised directly with a fake pool/connection (same style as
test_phrases_route.py); RLS visibility itself is integration-tested.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from autocomplete_service.deps import install_state
from autocomplete_service.routers.phrases import (
    PhraseListItemDTO,
    SnippetListItemDTO,
    list_phrases,
    list_snippets,
)

from auth import Claims

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


def _claims(roles=("tenant_admin",)) -> Claims:
    return Claims(
        sub=uuid4(), tid=uuid4(), roles=list(roles), scope="openid",
        sid="s", iss="test", aud="mdx-api", exp=2_000_000_000, iat=1,
    )


class _FakeTx:
    async def start(self) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...


class _FakeConn:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple]] = []
        self.fetches: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args) -> None:
        self.executed.append((sql, args))

    async def fetch(self, sql: str, *args) -> list[dict]:
        self.fetches.append((sql, args))
        return self.rows

    def transaction(self) -> _FakeTx:
        return _FakeTx()


class _Acquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class _State:
    def __init__(self, conn: _FakeConn) -> None:
        self.app_pool = _FakePool(conn)


def _phrase_row(**over) -> dict:
    row = {
        "id": uuid4(),
        "phrase": "скарги на головний біль",
        "language": "uk",
        "specialty": "neurology",
        "section_hint": None,
        "source": "tenant",
        "impression_count": 40,
        "acceptance_count": 9,
        "last_accepted_at": NOW,
        "enabled": True,
        "created_at": NOW,
        # sprint-21 provenance columns (ADR-0043)
        "review_state": "accepted",
        "tier": None,
        "source_kind": "authored",
        "corpus_release": None,
    }
    row.update(over)
    return row


async def test_list_phrases_maps_rows_and_scopes_rls():
    conn = _FakeConn([_phrase_row(), _phrase_row(source="system", last_accepted_at=None)])
    install_state(_State(conn))
    claims = _claims(roles=("clinician", "tenant_admin"))

    out = await list_phrases(claims, language="uk", specialty=None, source=None, limit=50)

    assert [type(o) for o in out] == [PhraseListItemDTO, PhraseListItemDTO]
    assert out[0].acceptance_count == 9
    assert out[1].source == "system"
    assert out[1].last_accepted_at is None
    # the RLS GUCs were set before the query, and the admin role won
    gucs = {a[0] for _, a in conn.executed if a}
    assert str(claims.sub) in gucs
    assert "tenant_admin" in gucs
    # filters reach the SQL: language bound as $1, limit last
    sql, args = conn.fetches[0]
    assert "AND language = $1" in sql
    assert args[0] == "uk"
    assert args[-1] == 50


async def test_list_phrases_filters_compose_in_order():
    conn = _FakeConn([])
    install_state(_State(conn))

    out = await list_phrases(
        _claims(), language="en", specialty="cardiology", source="user", limit=10
    )

    assert out == []
    sql, args = conn.fetches[0]
    assert "AND language = $1" in sql
    assert "AND specialty = $2" in sql
    assert "AND source = $3::autocomplete_source" in sql
    assert args == ("en", "cardiology", "user", 10)
    assert "ORDER BY updated_at DESC" in sql


async def test_list_snippets_maps_rows():
    row = {
        "id": uuid4(),
        "trigger": "vitals",
        "expansion": "АТ ___ мм рт.ст., ЧСС ___",
        "cursor_position": 3,
        "language": "uk",
        "source": "tenant",
        "enabled": True,
        "created_at": NOW,
    }
    conn = _FakeConn([row])
    install_state(_State(conn))

    out = await list_snippets(_claims(), language=None, source="tenant", limit=25)

    assert [type(o) for o in out] == [SnippetListItemDTO]
    assert out[0].trigger == "vitals"
    assert out[0].cursor_position == 3
    sql, args = conn.fetches[0]
    assert "FROM autocomplete_snippets" in sql
    assert args == ("tenant", 25)
