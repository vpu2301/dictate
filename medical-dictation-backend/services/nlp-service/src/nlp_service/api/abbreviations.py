"""Admin endpoints for the per-tenant abbreviation dictionary.

- ``GET /nlp/abbreviations`` — paginated; returns merged tenant + global rows.
- ``PUT /nlp/abbreviations`` — upsert one tenant row; audit logged.
- ``DELETE /nlp/abbreviations/{id}`` — remove one tenant row; audit logged.

Read available to any authenticated user (sprint 17 admin UI will
surface this); write requires the ``tenant_admin`` role per perms
matrix.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_kinds
from ..deps import get_state, requires
from ..domain import repository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/nlp", tags=["nlp-admin"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AbbreviationOut(_StrictModel):
    id: UUID | None
    tenant_id: UUID | None
    language: str
    expanded: str
    abbreviated: str
    direction: Literal["expand", "compact", "either"]
    domain: str | None
    case_sensitive: bool
    is_tenant_override: bool


class AbbreviationUpsertIn(_StrictModel):
    language: Literal["uk", "en"]
    expanded: str = Field(min_length=1, max_length=200)
    abbreviated: str = Field(min_length=1, max_length=50)
    direction: Literal["expand", "compact", "either"]
    domain: str | None = None
    case_sensitive: bool = True


@router.get(
    "/abbreviations",
    response_model=list[AbbreviationOut],
    summary="List the merged abbreviation dictionary (tenant + global).",
)
async def list_abbreviations(
    claims: Annotated[Claims, Depends(requires("nlp.read.abbreviations", "abbreviation"))],
    language: Annotated[Literal["uk", "en"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[AbbreviationOut]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        if language is None:
            rows = await conn.fetch(
                """
                SELECT id, tenant_id, language, expanded, abbreviated,
                       direction, domain, case_sensitive
                FROM abbreviation_dictionary
                ORDER BY tenant_id NULLS LAST, language, expanded
                LIMIT $1
                """,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, tenant_id, language, expanded, abbreviated,
                       direction, domain, case_sensitive
                FROM abbreviation_dictionary
                WHERE language = $1
                ORDER BY tenant_id NULLS LAST, expanded
                LIMIT $2
                """,
                language,
                limit,
            )
    return [
        AbbreviationOut(
            id=r["id"],
            tenant_id=r["tenant_id"],
            language=r["language"],
            expanded=r["expanded"],
            abbreviated=r["abbreviated"],
            direction=r["direction"],
            domain=r["domain"],
            case_sensitive=bool(r["case_sensitive"]),
            is_tenant_override=r["tenant_id"] is not None,
        )
        for r in rows
    ]


@router.put(
    "/abbreviations",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Insert or update one tenant-scoped abbreviation rule.",
)
async def upsert_abbreviation(
    body: AbbreviationUpsertIn,
    claims: Annotated[Claims, Depends(requires("nlp.write.abbreviations", "abbreviation"))],
) -> None:
    state = get_state()
    await repository.upsert_tenant_abbreviation(
        state.app_pool,
        tenant_id=claims.tid,
        language=body.language,
        expanded=body.expanded,
        abbreviated=body.abbreviated,
        direction=body.direction,
        domain=body.domain,
        case_sensitive=body.case_sensitive,
    )
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.ABBREVIATION_POLICY_SET,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="abbreviation",
        target_id=None,
        payload={
            "language": body.language,
            "expanded": body.expanded,
            "abbreviated": body.abbreviated,
            "direction": body.direction,
            "domain": body.domain,
        },
        severity=Severity.INFO,
    )


@router.delete(
    "/abbreviations/{abbreviation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one tenant-scoped abbreviation rule (global rules untouched).",
)
async def delete_abbreviation(
    abbreviation_id: UUID,
    claims: Annotated[Claims, Depends(requires("nlp.write.abbreviations", "abbreviation"))],
) -> None:
    state = get_state()
    deleted = await repository.delete_tenant_abbreviation(
        state.app_pool, tenant_id=claims.tid, abbreviation_id=abbreviation_id
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.ABBREVIATION_POLICY_DELETED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="abbreviation",
        target_id=str(abbreviation_id),
        payload={},
        severity=Severity.INFO,
    )
