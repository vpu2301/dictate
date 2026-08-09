"""Templates repository (sprint 06).

Every query is RLS-scoped via :func:`db.tenant_connection`. Reads see
own-tenant + system rows (tenant_id IS NULL). Writes are restricted to
own-tenant rows.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from template_models import (
    EditKind,
    TemplateDefinition,
    classify_edit,
)

logger = logging.getLogger(__name__)


# ── List + Get ──────────────────────────────────────────────────────


async def list_templates(
    conn: asyncpg.Connection,
    *,
    tenant_only: bool = False,
    specialty: str | None = None,
    language: str | None = None,
    include_deprecated: bool = False,
    limit: int = 50,
    cursor: tuple[datetime, UUID] | None = None,
) -> list[asyncpg.Record]:
    """Return metadata-only rows (no schema_jsonb).

    Cursor pagination on (updated_at, id) descending.
    """
    where: list[str] = []
    args: list[Any] = []
    if tenant_only:
        where.append("tenant_id = current_setting('app.tenant_id', true)::uuid")
    if specialty is not None:
        where.append(f"specialty = ${len(args) + 1}")
        args.append(specialty)
    if language is not None:
        where.append(f"language = ${len(args) + 1}")
        args.append(language)
    if not include_deprecated:
        where.append("status <> 'deprecated'")
    if cursor is not None:
        where.append(f"(updated_at, id) < (${len(args) + 1}, ${len(args) + 2})")
        args.append(cursor[0])
        args.append(cursor[1])
    args.append(limit)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return list(
        await conn.fetch(
            f"""
            SELECT id, tenant_id, parent_template_id, code, name,
                   language, specialty, schema_version, is_system,
                   status, created_at, updated_at
            FROM templates
            {where_sql}
            ORDER BY updated_at DESC, id DESC
            LIMIT ${len(args)}
            """,
            *args,
        )
    )


async def get_template(conn: asyncpg.Connection, *, template_id: UUID) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT id, tenant_id, parent_template_id, code, name, language,
               specialty, schema_version, is_system, status,
               schema_jsonb, created_at, updated_at
        FROM templates
        WHERE id = $1
        """,
        template_id,
    )


# ── Clone ───────────────────────────────────────────────────────────


async def clone_template(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    source_id: UUID,
    new_name: str | None,
    new_code: str | None,
) -> UUID | None:
    """Read source (visible via RLS); insert tenant-scoped copy.

    Returns the new id, or None if source not visible.
    """
    source = await conn.fetchrow(
        "SELECT * FROM templates WHERE id = $1",
        source_id,
    )
    if source is None:
        return None

    schema_jsonb = source["schema_jsonb"]
    if isinstance(schema_jsonb, str):
        schema_jsonb = json.loads(schema_jsonb)
    code = new_code or f"{source['code']}_clone"
    name = new_name or f"{source['name']} (clone)"
    # Update embedded code/name to match the row.
    if isinstance(schema_jsonb, dict):
        schema_jsonb = dict(schema_jsonb)
        schema_jsonb["code"] = code
        schema_jsonb["name"] = name

    row_id = await conn.fetchval(
        """
        INSERT INTO templates
            (tenant_id, parent_template_id, code, name, language, specialty,
             schema_version, is_system, status, schema_jsonb)
        VALUES ($1, $2, $3, $4, $5, $6, 1, FALSE, 'draft', $7::jsonb)
        RETURNING id
        """,
        tenant_id,
        source["id"],
        code,
        name,
        source["language"],
        source["specialty"],
        json.dumps(schema_jsonb),
    )
    return row_id  # type: ignore[no-any-return]


async def create_template(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    definition: TemplateDefinition,
) -> UUID:
    """Insert a brand-new tenant template (M1·A4).

    Mirrors the structural-edit INSERT but with no parent link: a
    plain draft (``parent_template_id=NULL, is_system=FALSE,
    status='draft', schema_version=1``). Returns the new id.
    """
    new_id = await conn.fetchval(
        """
        INSERT INTO templates
            (tenant_id, parent_template_id, code, name, language, specialty,
             schema_version, is_system, status, schema_jsonb)
        VALUES ($1, NULL, $2, $3, $4, $5, 1, FALSE, 'draft', $6::jsonb)
        RETURNING id
        """,
        tenant_id,
        definition.code,
        definition.name,
        definition.language,
        definition.specialty,
        json.dumps(definition.model_dump(mode="json")),
    )
    return new_id  # type: ignore[no-any-return]


# ── Update (cosmetic vs structural) ─────────────────────────────────


async def update_template(
    conn: asyncpg.Connection,
    *,
    template_id: UUID,
    new_definition: TemplateDefinition,
) -> tuple[UUID, EditKind]:
    """Apply ADR-0016 rule: cosmetic = in-place + version bump;
    structural = new row + parent_template_id link.

    Returns (resulting_id, edit_kind).
    """
    current = await conn.fetchrow(
        "SELECT * FROM templates WHERE id = $1 FOR UPDATE",
        template_id,
    )
    if current is None:
        raise ValueError("template not found / not visible")
    raw = current["schema_jsonb"]
    if isinstance(raw, str):
        raw = json.loads(raw)
    old_def = TemplateDefinition.model_validate(raw)

    classification = classify_edit(old_def, new_definition)
    if classification.kind == EditKind.NO_CHANGE:
        return template_id, EditKind.NO_CHANGE

    if classification.kind == EditKind.COSMETIC:
        new_version = int(current["schema_version"]) + 1
        await conn.execute(
            """
            UPDATE templates
            SET name           = $2,
                schema_version = $3,
                schema_jsonb   = $4::jsonb,
                updated_at     = now()
            WHERE id = $1
            """,
            template_id,
            new_definition.name,
            new_version,
            json.dumps(new_definition.model_dump(mode="json")),
        )
        return template_id, EditKind.COSMETIC

    # Structural — INSERT new row with parent_template_id link.
    new_id = await conn.fetchval(
        """
        INSERT INTO templates
            (tenant_id, parent_template_id, code, name, language, specialty,
             schema_version, is_system, status, schema_jsonb)
        VALUES ($1, $2, $3, $4, $5, $6, 1, FALSE, 'draft', $7::jsonb)
        RETURNING id
        """,
        current["tenant_id"],
        current["id"],
        new_definition.code,
        new_definition.name,
        new_definition.language,
        new_definition.specialty,
        json.dumps(new_definition.model_dump(mode="json")),
    )
    return new_id, EditKind.STRUCTURAL  # type: ignore[no-any-return,return-value]


# ── Deprecate (soft-delete) ─────────────────────────────────────────


async def deprecate_template(conn: asyncpg.Connection, *, template_id: UUID) -> str:
    """Set status='deprecated' if no referencing report exists.

    Returns "deprecated" on success, "in_use" if reports reference,
    "not_found" if not visible.
    """
    row = await conn.fetchrow(
        "SELECT id, status FROM templates WHERE id = $1 FOR UPDATE",
        template_id,
    )
    if row is None:
        return "not_found"
    if row["status"] == "deprecated":
        return "deprecated"  # idempotent

    # Sprint-17 semantics: only LIVE DRAFTS block deprecation — they are
    # the rows an admin can still re-bind to a successor. Finalized,
    # signed, amended and cancelled reports keep their historical
    # template binding forever ("existing reports keep it"); the FK is
    # ON DELETE RESTRICT and deprecation is a soft status flip, so
    # history is never endangered.
    try:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM reports WHERE template_id = $1 AND status = 'draft'",
            template_id,
        )
        if n and int(n) > 0:
            return "in_use"
    except asyncpg.UndefinedTableError:
        pass  # sprint-8 hasn't run yet

    await conn.execute(
        "UPDATE templates SET status = 'deprecated' WHERE id = $1",
        template_id,
    )
    return "deprecated"


# ── Bound reports + re-bind (sprint-17 admin console) ───────────────


async def list_bound_reports(
    conn: asyncpg.Connection,
    *,
    template_id: UUID,
    limit: int = 50,
) -> list[asyncpg.Record]:
    """PHI-free listing of reports referencing a template.

    Deliberately selects ONLY id/status/timestamps: the caller holds
    ``template.update``, not ``report.read`` (tenant_admin is excluded
    from clinical reads by the Admin ⟂ PHI separation), so no title,
    patient or author fields may appear here.
    """
    return list(
        await conn.fetch(
            """
            SELECT id, status, created_at, updated_at
            FROM reports
            WHERE template_id = $1
            ORDER BY updated_at DESC, id DESC
            LIMIT $2
            """,
            template_id,
            limit,
        )
    )


async def rebind_report(
    conn: asyncpg.Connection,
    *,
    template_id: UUID,
    report_id: UUID,
    to_template_id: UUID,
) -> str:
    """Move ONE draft report from ``template_id`` to a successor.

    Outcome codes: ``ok``, ``template_not_found``, ``report_not_found``,
    ``not_bound``, ``not_draft``, ``same_template``,
    ``target_not_found``, ``target_deprecated``, ``language_mismatch``.

    Only ``status='draft'`` reports are movable: finalized/signed/
    amended rows are immutable history and keep their template;
    cancelled rows are soft-deleted. ``template_schema_version`` is
    refreshed to the target's so the draft renders against the schema
    it is now bound to.
    """
    source = await conn.fetchrow(
        "SELECT id, language FROM templates WHERE id = $1",
        template_id,
    )
    if source is None:
        return "template_not_found"

    report = await conn.fetchrow(
        "SELECT id, status, template_id FROM reports WHERE id = $1 FOR UPDATE",
        report_id,
    )
    if report is None:
        return "report_not_found"
    if report["template_id"] != template_id:
        return "not_bound"
    if report["status"] != "draft":
        return "not_draft"
    if to_template_id == template_id:
        return "same_template"

    target = await conn.fetchrow(
        "SELECT id, status, language, schema_version FROM templates WHERE id = $1",
        to_template_id,
    )
    if target is None:
        return "target_not_found"
    if target["status"] == "deprecated":
        return "target_deprecated"
    if target["language"] != source["language"]:
        return "language_mismatch"

    await conn.execute(
        """
        UPDATE reports
        SET template_id = $2,
            template_schema_version = $3,
            updated_at = now()
        WHERE id = $1
        """,
        report_id,
        to_template_id,
        int(target["schema_version"]),
    )
    return "ok"


# ── Section prompt lookup (dictation hot path) ──────────────────────


def section_prompt_from_jsonb(
    schema_jsonb: dict[str, Any] | str, section_id: str
) -> tuple[str, str, str] | None:
    """Return (prompt, language, section_name) or None if not found."""
    raw: dict[str, Any] = (
        json.loads(schema_jsonb) if isinstance(schema_jsonb, str) else schema_jsonb
    )
    for section in raw.get("sections", []):
        if section.get("id") == section_id:
            return (
                section.get("asr_prompt", ""),
                raw.get("language", "uk"),
                section.get("name", section_id),
            )
    return None
