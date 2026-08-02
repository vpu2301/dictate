#!/usr/bin/env python3
"""Sprint-15 CI gate — validate the medical synonym seed corpus (ADR-0038).

Checks ``infra/seeds/synonyms/uk-medical-synonyms.json``:

- shape: {version, groups: [{id, uk: [...], en: [...]}]}
- group ids unique, positive ints
- every group has >= 2 terms total (a 1-term group expands nothing)
  and >= 1 uk term
- term constraints mirroring the DB CHECKs (migration 0063):
  1..120 chars after trim, no leading/trailing whitespace, no control chars
- duplicate terms within a group (case-insensitive)
- uk typography: Ukrainian text uses the ’ apostrophe or ASCII ' consistently
  with the DB normalizer (both normalize identically under 'simple'; we only
  reject control chars)
- the generated migration 0064 is IN SYNC with the fixture (term count +
  every (group, term) pair present) — regeneration drift fails CI.

Exit 1 on any failure, with row-level reasons.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE = ROOT / "infra/seeds/synonyms/uk-medical-synonyms.json"
MIGRATION = ROOT / "infra/postgres/migrations/0064_seed_medical_synonyms.sql"

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def main() -> int:
    errors: list[str] = []
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        errors.append("fixture version must be 1")
    groups = data.get("groups", [])
    seen_ids: set[int] = set()
    pairs: set[tuple[int, str]] = set()

    for group in groups:
        gid = group.get("id")
        where = f"group id={gid!r}"
        if not isinstance(gid, int) or gid <= 0:
            errors.append(f"{where}: id must be a positive int")
            continue
        if gid in seen_ids:
            errors.append(f"{where}: duplicate group id")
        seen_ids.add(gid)

        terms = [(lang, t) for lang in ("uk", "en") for t in group.get(lang, [])]
        if len(terms) < 2:
            errors.append(f"{where}: needs >= 2 terms to expand anything")
        if not group.get("uk"):
            errors.append(f"{where}: needs >= 1 uk term")
        lowered: set[str] = set()
        for lang, term in terms:
            twhere = f"{where} term {term!r}"
            if not isinstance(term, str) or not (1 <= len(term.strip()) <= 120):
                errors.append(f"{twhere}: length must be 1..120 after trim")
                continue
            if term != term.strip():
                errors.append(f"{twhere}: leading/trailing whitespace")
            if _CTRL.search(term):
                errors.append(f"{twhere}: control characters")
            key = f"{lang}:{term.lower()}"
            if key in lowered:
                errors.append(f"{twhere}: duplicate within group")
            lowered.add(key)
            pairs.add((gid, term))

    # Migration sync: every fixture (group, term) must appear in 0064.
    sql = MIGRATION.read_text(encoding="utf-8")
    for gid, term in sorted(pairs):
        gid_uuid = f"00000000-0000-4000-a000-{gid:012d}"
        escaped = term.replace("'", "''")
        if f"('{gid_uuid}', '{escaped}'," not in sql:
            errors.append(
                f"migration 0064 missing ({gid_uuid}, {term!r}) — "
                "run scripts/dev/gen-synonym-seed.py"
            )
    sql_rows = sql.count("('00000000-0000-4000-a000-")
    if sql_rows != len(pairs):
        errors.append(
            f"migration 0064 has {sql_rows} seeded rows, fixture has {len(pairs)} — "
            "run scripts/dev/gen-synonym-seed.py"
        )

    if errors:
        print(f"check_synonym_corpus: {len(errors)} problem(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(
        f"check_synonym_corpus: OK ({len(groups)} groups, {len(pairs)} terms, "
        "fixture ↔ migration in sync)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
