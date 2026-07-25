#!/usr/bin/env python3
"""Load / refresh the ICD-10 (МКХ-10) reference table — sprint 13.

Idempotent: re-running with the same file reports zero changes. Rows
are validated before anything is written; a single bad row fails the
whole load (non-zero exit) with the offending line numbers.

Input is CSV with a header row::

    code,display_uk,display_en,parent_code,chapter,is_leaf

``display_en``, ``parent_code``, ``chapter`` and ``is_leaf`` are
optional per row (empty ⇒ '', NULL, '', true respectively).

The committed fixture (``infra/seeds/icd10/fixture.csv``) powers CI and
dev. Loading the FULL МКХ-10-АМ table is an ops step — see
``docs/runbooks/icd10.md`` for the data-acquisition position.

Run::

    uv run python scripts/load-icd10.py --file infra/seeds/icd10/fixture.csv
    uv run python scripts/load-icd10.py --file <moz-export.csv> --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

REPO = Path(__file__).resolve().parents[1]
DEFAULT_FILE = REPO / "infra" / "seeds" / "icd10" / "fixture.csv"
DEFAULT_DSN = "postgresql://tenant_writer:tenant_writer@localhost:5432/medical_dictation"

# WHO ICD-10 / МКХ-10-АМ code dialect. Ukrainian МКХ-10-АМ keeps the WHO
# letter+2-digit stem and extends the subdivision after the dot (up to 4
# alphanumerics; AM adds digits, not new stem shapes). Mirrors the CHECK
# constraint in migration 0054 and Icd10Code in libs/report_models —
# widen all three together if a real МОЗ export shows other forms.
CODE_RE = re.compile(r"^[A-Z][0-9]{2}(\.[0-9A-Z]{1,4})?$")

_TRUE = {"true", "t", "1", "yes", "y"}
_FALSE = {"false", "f", "0", "no", "n"}


@dataclass(frozen=True, slots=True)
class Row:
    code: str
    display_uk: str
    display_en: str
    parent_code: str | None
    chapter: str
    is_leaf: bool


def parse_csv(path: Path) -> tuple[list[Row], list[str]]:
    """Parse + validate. Returns (rows, errors); errors carry line numbers."""
    rows: list[Row] = []
    errors: list[str] = []
    seen: dict[str, int] = {}

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = {"code", "display_uk"} - set(reader.fieldnames or [])
        if missing:
            return [], [f"{path.name}: missing required column(s): {', '.join(sorted(missing))}"]

        for raw in reader:
            line = reader.line_num
            code = (raw.get("code") or "").strip().upper()
            display_uk = (raw.get("display_uk") or "").strip()
            display_en = (raw.get("display_en") or "").strip()
            parent = (raw.get("parent_code") or "").strip().upper() or None
            chapter = (raw.get("chapter") or "").strip()
            is_leaf_raw = (raw.get("is_leaf") or "").strip().lower()

            if not code:
                errors.append(f"line {line}: empty code")
                continue
            if not CODE_RE.match(code):
                errors.append(f"line {line}: code {code!r} does not match {CODE_RE.pattern}")
                continue
            if code in seen:
                errors.append(
                    f"line {line}: duplicate code {code!r} (first seen line {seen[code]})"
                )
                continue
            seen[code] = line
            if not display_uk:
                errors.append(f"line {line}: code {code!r} has empty display_uk")
                continue
            if parent is not None and not CODE_RE.match(parent):
                errors.append(f"line {line}: parent_code {parent!r} is not a valid code")
                continue
            if parent == code:
                errors.append(f"line {line}: code {code!r} is its own parent")
                continue
            if is_leaf_raw == "":
                is_leaf = True  # column omitted ⇒ selectable leaf
            elif is_leaf_raw in _TRUE:
                is_leaf = True
            elif is_leaf_raw in _FALSE:
                is_leaf = False
            else:
                errors.append(f"line {line}: is_leaf {is_leaf_raw!r} is not a boolean")
                continue

            rows.append(
                Row(
                    code=code,
                    display_uk=display_uk,
                    display_en=display_en,
                    parent_code=parent,
                    chapter=chapter,
                    is_leaf=is_leaf,
                )
            )

    # Parent existence: within the file, or already in the table (checked
    # at load time by the FK — this catches the common authoring error).
    known = set(seen)
    for row in rows:
        if row.parent_code is not None and row.parent_code not in known:
            errors.append(
                f"code {row.code!r}: parent_code {row.parent_code!r} is not present in the file"
            )
    return rows, errors


def order_parents_first(rows: list[Row]) -> list[Row]:
    """Topological-ish sort so a parent is always inserted before its
    children (the FK is immediate, not deferred)."""
    by_code = {r.code: r for r in rows}
    ordered: list[Row] = []
    placed: set[str] = set()

    def place(row: Row, chain: frozenset[str]) -> None:
        if row.code in placed:
            return
        parent = row.parent_code
        if parent is not None and parent in by_code and parent not in chain:
            place(by_code[parent], chain | {row.code})
        if row.code not in placed:
            placed.add(row.code)
            ordered.append(row)

    for row in rows:
        place(row, frozenset())
    return ordered


class _RollbackError(Exception):
    """Internal: unwinds the transaction for --dry-run."""


async def load(dsn: str, rows: list[Row], *, dry_run: bool) -> tuple[int, int, int]:
    """Upsert in one transaction. Returns (inserted, updated, unchanged)."""
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            existing = {
                r["code"]: (
                    r["display_uk"],
                    r["display_en"],
                    r["parent_code"],
                    r["chapter"],
                    r["is_leaf"],
                )
                for r in await conn.fetch(
                    "SELECT code, display_uk, display_en, parent_code, chapter, is_leaf"
                    " FROM icd10_codes"
                )
            }
            inserted = updated = unchanged = 0
            for row in rows:
                current = existing.get(row.code)
                incoming = (
                    row.display_uk,
                    row.display_en,
                    row.parent_code,
                    row.chapter,
                    row.is_leaf,
                )
                if current is None:
                    inserted += 1
                elif current == incoming:
                    unchanged += 1
                    continue
                else:
                    updated += 1
                if not dry_run:
                    await conn.execute(
                        """
                        INSERT INTO icd10_codes
                            (code, display_uk, display_en, parent_code, chapter, is_leaf)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (code) DO UPDATE SET
                            display_uk  = EXCLUDED.display_uk,
                            display_en  = EXCLUDED.display_en,
                            parent_code = EXCLUDED.parent_code,
                            chapter     = EXCLUDED.chapter,
                            is_leaf     = EXCLUDED.is_leaf
                        """,
                        row.code,
                        row.display_uk,
                        row.display_en,
                        row.parent_code,
                        row.chapter,
                        row.is_leaf,
                    )
            if dry_run:
                raise _RollbackError
            return inserted, updated, unchanged
    except _RollbackError:
        return inserted, updated, unchanged
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate + report counts and a sample; write nothing",
    )
    args = parser.parse_args()

    if not args.file.exists():
        print(f"error: {args.file} does not exist", file=sys.stderr)
        return 2

    rows, errors = parse_csv(args.file)
    if errors:
        print(f"=== {len(errors)} INVALID ROW(S) in {args.file.name} ===", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1
    if not rows:
        print(f"error: {args.file.name} contains no rows", file=sys.stderr)
        return 1

    ordered = order_parents_first(rows)
    leaves = sum(1 for r in ordered if r.is_leaf)
    chapters = sorted({r.chapter for r in ordered if r.chapter})
    print(
        f"parsed {len(ordered)} codes from {args.file.name} "
        f"({leaves} leaf, {len(ordered) - leaves} heading; {len(chapters)} chapters)"
    )
    if args.dry_run:
        print("sample:")
        for row in ordered[:5]:
            print(f"  {row.code:<8} {row.display_uk[:60]}  (leaf={row.is_leaf})")

    inserted, updated, unchanged = asyncio.run(load(args.dsn, ordered, dry_run=args.dry_run))
    verb = "would insert/update" if args.dry_run else "inserted/updated"
    print(f"{verb}: {inserted} inserted, {updated} updated, {unchanged} unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
