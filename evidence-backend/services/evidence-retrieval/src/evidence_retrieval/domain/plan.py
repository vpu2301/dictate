"""Filter compilation: ONE Filters object compiles to both engines
(equivalence is property-tested on the probe fixtures, spec D3)."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authority: list[str] | None = None
    jurisdiction: str | None = None
    specialty: list[str] | None = None
    date_from: date | None = None
    date_to: date | None = None
    include_superseded: bool = False


def compile_sql(filters: Filters, *, first_param: int) -> tuple[str, list[Any]]:
    """WHERE fragments against the documents table (alias d), numbered from
    $first_param."""
    clauses: list[str] = []
    params: list[Any] = []
    n = first_param
    if filters.authority:
        clauses.append(f"d.source_authority = ANY(${n}::text[])")
        params.append(filters.authority)
        n += 1
    if filters.jurisdiction:
        clauses.append(f"d.jurisdiction = ${n}")
        params.append(filters.jurisdiction)
        n += 1
    if filters.specialty:
        clauses.append(f"d.specialty && ${n}::text[]")
        params.append(filters.specialty)
        n += 1
    if filters.date_from:
        clauses.append(f"d.published_at >= ${n}")
        params.append(filters.date_from)
        n += 1
    if filters.date_to:
        clauses.append(f"d.published_at <= ${n}")
        params.append(filters.date_to)
        n += 1
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def compile_opensearch(filters: Filters, *, tenant_id: UUID) -> list[dict[str, Any]]:
    """filter clauses for the OpenSearch bool query — same semantics as
    compile_sql over the projected fields."""
    clauses: list[dict[str, Any]] = [
        {"term": {"tenant_id": str(tenant_id)}},
        {"term": {"retracted": False}},
    ]
    if filters.authority:
        clauses.append({"terms": {"source_authority": filters.authority}})
    if filters.jurisdiction:
        clauses.append({"term": {"jurisdiction": filters.jurisdiction}})
    if filters.specialty:
        clauses.append({"terms": {"specialty": filters.specialty}})
    date_range: dict[str, str] = {}
    if filters.date_from:
        date_range["gte"] = filters.date_from.isoformat()
    if filters.date_to:
        date_range["lte"] = filters.date_to.isoformat()
    if date_range:
        clauses.append({"range": {"published_at": date_range}})
    return clauses
