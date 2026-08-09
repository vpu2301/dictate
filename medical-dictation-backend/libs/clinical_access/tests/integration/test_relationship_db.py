"""The relationship predicate's SQL, run against a real PostgreSQL.

    RUN_DB_INTEGRATION=1 pytest libs/clinical_access/tests/integration

Why this file exists. `relationship_with_patient` sits in front of EVERY
patient read in the product, and its unit tests drive a hand-written fake
connection that records SQL strings and answers from Python lists. That fake
happily accepted a query PostgreSQL refuses to parse:

    SELECT 'author'::text AS basis, 1 AS rank FROM reports WHERE ... LIMIT 1
    UNION ALL
    SELECT ...

A bare ``LIMIT`` in a branch of a set operation is read as a LIMIT on the whole
UNION, so the parser stops at ``UNION`` — "syntax error at or near UNION". Every
``GET /patients/{id}`` answered 500, which the browser reports as a CORS
failure (an unhandled 500 carries no ``Access-Control-Allow-Origin``), so the
frontend showed "Сервіс недоступний" and no clinician could open a record.

A fake connection can never catch that. This one asks the database.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest
from clinical_access.relationship import (
    NO_RELATIONSHIP,
    RelationshipBasis,
    relationship_with_patient,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs `make dev-up && make migrate-up && make seed`",
)

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")
SU_DSN = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"


@pytest.fixture
async def conn():
    c = await asyncpg.connect(SU_DSN)
    try:
        yield c
    finally:
        await c.close()


async def test_the_query_parses_at_all(conn: asyncpg.Connection) -> None:
    """The regression, stated as plainly as it can be.

    Two ids that match nothing: the answer is "no relationship". Reaching that
    answer at all means PostgreSQL parsed and planned the statement, which is
    the thing that was broken — a syntax error is raised at prepare time, long
    before any row is considered, so an empty database is enough to catch it.
    """
    rel = await relationship_with_patient(conn, user_sub=uuid4(), patient_id=uuid4())
    assert rel == NO_RELATIONSHIP
    assert not rel.exists


async def test_authorship_is_a_relationship(conn: asyncpg.Connection) -> None:
    """And the query does not merely parse — it answers correctly."""
    tenant_id, patient_id, user_sub = await _fixture_patient(conn)
    report_id = uuid4()
    await conn.execute(
        """
        INSERT INTO reports (id, tenant_id, patient_id, primary_author_id, status, code)
        VALUES ($1, $2, $3, $4, 'draft', $5)
        """,
        report_id, tenant_id, patient_id, user_sub, f"ITEST-{str(report_id)[:8]}",
    )
    try:
        rel = await relationship_with_patient(conn, user_sub=user_sub, patient_id=patient_id)
        assert rel.exists
        assert rel.basis is RelationshipBasis.AUTHOR
    finally:
        await conn.execute("DELETE FROM reports WHERE id = $1", report_id)


async def test_a_stranger_has_none(conn: asyncpg.Connection) -> None:
    """The half that break-glass depends on: an unrelated principal is refused,
    which is what turns into the interstitial in the client."""
    tenant_id, patient_id, _user_sub = await _fixture_patient(conn)
    assert tenant_id  # the row exists; the point is the OTHER user
    rel = await relationship_with_patient(conn, user_sub=uuid4(), patient_id=patient_id)
    assert not rel.exists
    assert rel.basis is RelationshipBasis.NONE


async def test_specificity_ordering_survives_the_parentheses(
    conn: asyncpg.Connection,
) -> None:
    """Authorship outranks having opened an encounter.

    Worth its own test because the fix parenthesised each UNION branch, and a
    parenthesised branch is exactly where an ORDER BY can end up bound to the
    wrong scope. The strongest basis that holds must still be the one reported.
    """
    tenant_id, patient_id, user_sub = await _fixture_patient(conn)
    report_id, encounter_id = uuid4(), uuid4()
    await conn.execute(
        """
        INSERT INTO encounters (id, tenant_id, patient_id, created_by, status, kind)
        VALUES ($1, $2, $3, $4, 'in_progress', 'visit')
        """,
        encounter_id, tenant_id, patient_id, user_sub,
    )
    await conn.execute(
        """
        INSERT INTO reports (id, tenant_id, patient_id, primary_author_id, status, code)
        VALUES ($1, $2, $3, $4, 'draft', $5)
        """,
        report_id, tenant_id, patient_id, user_sub, f"ITEST-{str(report_id)[:8]}",
    )
    try:
        rel = await relationship_with_patient(conn, user_sub=user_sub, patient_id=patient_id)
        assert rel.basis is RelationshipBasis.AUTHOR, "author (rank 1) outranks encounter (rank 3)"
    finally:
        await conn.execute("DELETE FROM reports WHERE id = $1", report_id)
        await conn.execute("DELETE FROM encounters WHERE id = $1", encounter_id)


async def _fixture_patient(conn: asyncpg.Connection) -> tuple[UUID, UUID, UUID]:
    """(tenant_id, patient_id, user_sub) from the dev seeds."""
    row = await conn.fetchrow("SELECT id, tenant_id FROM patients LIMIT 1")
    if row is None:
        pytest.skip("no seeded patients — run `make seed`")
    user = await conn.fetchrow("SELECT sub FROM users LIMIT 1")
    if user is None:
        pytest.skip("no seeded users — run `make seed`")
    return row["tenant_id"], row["id"], user["sub"]
