#!/usr/bin/env python3
"""Regenerate infra/postgres/migrations/0064_seed_medical_synonyms.sql from
infra/seeds/synonyms/uk-medical-synonyms.json (the fixture is the source of
truth; the migration is its frozen snapshot — the runner checksums applied
migrations, so corpus GROWTH goes into a NEW migration, never an edit here).

Group UUIDs are deterministic (00000000-0000-4000-a000-<id>) so the down
migration can delete exactly the seeded rows by prefix. `lexemes` is computed
IN SQL via to_tsvector('simple', …) — identical normalization to query time.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE = ROOT / "infra/seeds/synonyms/uk-medical-synonyms.json"
OUT = ROOT / "infra/postgres/migrations/0064_seed_medical_synonyms.sql"

HEADER = """\
-- Sprint 15 (ADR-0038): system synonym seed — GENERATED, do not hand-edit.
-- Source fixture: infra/seeds/synonyms/uk-medical-synonyms.json
-- Regenerate with: uv run python scripts/dev/gen-synonym-seed.py
-- (Corpus growth = new fixture entries + a NEW migration; this file is
-- checksummed once applied.)

INSERT INTO medical_synonyms (group_id, term, lexemes, language, source)
SELECT gid::uuid,
       term,
       tsvector_to_array(to_tsvector('simple', term)),
       lang,
       'system'
FROM (VALUES
"""

FOOTER = """\
) AS v(gid, term, lang)
ON CONFLICT DO NOTHING;
"""


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def main() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows: list[str] = []
    for group in data["groups"]:
        gid = f"00000000-0000-4000-a000-{group['id']:012d}"
        for lang in ("uk", "en"):
            for term in group.get(lang, []):
                rows.append(f"    ({_sql_str(gid)}, {_sql_str(term)}, {_sql_str(lang)})")
    OUT.write_text(HEADER + ",\n".join(rows) + "\n" + FOOTER, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(rows)} terms, {len(data['groups'])} groups)")


if __name__ == "__main__":
    main()
