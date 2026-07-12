"""Step-06 §8 — RLS write-authority mapping through the route handlers.

The DB is the authority matrix; the handlers only map its rejections:
clinician posting source='tenant' → 403 forbidden_scope; tenant_admin
same body → 201; user A deleting user B's row → 404 (no existence
oracle). Audit events land on the chain for successful writes.

Skipped unless ``RUN_DB_INTEGRATION=1``.
"""

from __future__ import annotations

import os
from uuid import UUID

import asyncpg
import pytest
from autocomplete_service.deps import install_state
from autocomplete_service.routers.phrases import (
    CreatePhraseRequest,
    create_phrase,
    delete_phrase,
)
from fastapi import HTTPException

from audit import AuditWriter
from auth import Claims
from db import create_pool

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs `make dev-up && make migrate-up`",
)

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")
APP_DSN = f"postgresql://app_role:app_role@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
AUDIT_DSN = (
    f"postgresql://audit_writer:audit_writer@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
)
SU_DSN = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"

TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
MARK = "itest-s06"


def _claims(sub: UUID, role: str) -> Claims:
    return Claims(
        sub=sub, tid=TENANT_A, roles=[role], scope="openid",
        sid="s", iss="test", aud="mdx-api", exp=2_000_000_000, iat=1,
    )


class _Metric:
    def add(self, *a, **k):
        pass


class _Limiter:
    async def check(self, *, user_id):
        return True, 0


class _FakeTrieCache:
    async def bump_version_tag(self, *, tenant_id):
        pass


class _State:
    def __init__(self, app_pool, audit_writer) -> None:
        self.app_pool = app_pool
        self.audit_writer = audit_writer
        self.pii_rejections_metric = _Metric()
        self.phrase_rate_limiter = _Limiter()
        self.trie_cache = _FakeTrieCache()


@pytest.fixture
async def env():
    app_pool = await create_pool(APP_DSN, application_name="s06-itest", min_size=1, max_size=2)
    audit_pool = await create_pool(AUDIT_DSN, application_name="s06-itest-a", min_size=1, max_size=2)
    su = await asyncpg.connect(SU_DSN)
    users = await su.fetch("SELECT sub FROM users WHERE tenant_id = $1 LIMIT 2", TENANT_A)
    if len(users) < 2:
        pytest.skip("needs >= 2 seeded tenant-A users")
    state = _State(app_pool, AuditWriter(audit_pool))
    install_state(state)
    yield state, su, users[0]["sub"], users[1]["sub"]
    await su.execute("DELETE FROM autocomplete_phrases WHERE phrase LIKE $1", f"{MARK}%")
    await su.close()
    await app_pool.close()
    await audit_pool.close()


async def test_clinician_tenant_scope_maps_to_403(env):
    state, su, user_a, _ = env
    body = CreatePhraseRequest(phrase=f"{MARK} клінічна фраза", language="uk", source="tenant")
    with pytest.raises(HTTPException) as exc:
        await create_phrase(body, _claims(user_a, "clinician"))
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "forbidden_scope"
    n = await su.fetchval(
        "SELECT count(*) FROM autocomplete_phrases WHERE phrase LIKE $1", f"{MARK}%"
    )
    assert n == 0


async def test_tenant_admin_same_body_gets_201_and_audit(env):
    state, su, user_a, _ = env
    body = CreatePhraseRequest(phrase=f"{MARK} клінічна фраза", language="uk", source="tenant")
    dto = await create_phrase(body, _claims(user_a, "tenant_admin"))
    assert dto.source == "tenant"
    audit_kind = await su.fetchval(
        "SELECT kind FROM audit.events WHERE tenant_id=$1 "
        "AND kind='autocomplete.phrase.created' ORDER BY seq DESC LIMIT 1",
        TENANT_A,
    )
    assert audit_kind == "autocomplete.phrase.created"


async def test_user_a_deleting_user_b_row_gets_404(env):
    state, su, user_a, user_b = env
    dto = await create_phrase(
        CreatePhraseRequest(phrase=f"{MARK} моя фраза юзера б", language="uk"),
        _claims(user_b, "clinician"),
    )
    with pytest.raises(HTTPException) as exc:
        await delete_phrase(dto.id, _claims(user_a, "clinician"))
    assert exc.value.status_code == 404  # indistinguishable from nonexistent
    enabled = await su.fetchval(
        "SELECT enabled FROM autocomplete_phrases WHERE id=$1", dto.id
    )
    assert enabled is True  # untouched
    # owner CAN delete it
    await delete_phrase(dto.id, _claims(user_b, "clinician"))
    enabled = await su.fetchval(
        "SELECT enabled FROM autocomplete_phrases WHERE id=$1", dto.id
    )
    assert enabled is False
