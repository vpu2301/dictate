"""Release packaging — an immutable, bisectable corpus artifact (ADR-0043 §5).

Mirrors the eval/corpus/v1 manifest pattern: deterministic CSV of the exact
accepted row set + manifest.json with SHA-256. Same rows → same SHA, always
(tested in tests/unit/test_release.py).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReleaseRow:
    phrase: str
    language: str
    specialty: str | None
    section_hint: str | None
    source_kind: str
    source_ref: str | None
    tier: int | None
    review_engine: str | None
    risk_flags: tuple[str, ...]


CSV_HEADER = (
    "phrase",
    "language",
    "specialty",
    "section_hint",
    "source_kind",
    "source_ref",
    "tier",
    "review_engine",
    "risk_flags",
)


def render_release_csv(rows: list[ReleaseRow]) -> str:
    """Deterministic: sorted by (language, phrase), LF line endings."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for row in sorted(rows, key=lambda r: (r.language, r.phrase)):
        writer.writerow(
            [
                row.phrase,
                row.language,
                row.specialty or "",
                row.section_hint or "",
                row.source_kind,
                row.source_ref or "",
                "" if row.tier is None else str(row.tier),
                row.review_engine or "",
                ";".join(row.risk_flags),
            ]
        )
    return buf.getvalue()


def build_manifest(
    *,
    version: str,
    rows: list[ReleaseRow],
    fluency_filter: str,
    notes: str = "",
) -> tuple[str, str, str]:
    """Returns (csv_text, manifest_json, manifest_sha256).

    The stored SHA is over the manifest JSON (which itself pins the CSV
    SHA), so one hex string authenticates the whole artifact."""
    csv_text = render_release_csv(rows)
    csv_sha = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    manifest = {
        "corpus_release": version,
        "schema_version": 1,
        "phrase_count": len(rows),
        "fluency_filter": fluency_filter,
        "notes": notes,
        "files": {"phrases.csv": {"sha256": csv_sha}},
    }
    manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    manifest_sha = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    return csv_text, manifest_json, manifest_sha
