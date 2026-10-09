"""Terminology source parsing (sprint plan §3).

Every importer records `source_ref` = '<dataset>:<version>:<sha256>' and
ends in phrase-ification (domain/phrasing.py). Licence basis per source:
docs/sprint-21/EXPLORE.md §2 — production import runs only after the DPO
countersign.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Term:
    text: str
    section: str  # which phrasing template set applies ('diagnosis', 'plan', …)


def dataset_source_ref(dataset: str, version: str, raw_bytes: bytes) -> str:
    return f"{dataset}:{version}:{hashlib.sha256(raw_bytes).hexdigest()}"


def parse_drlz_csv(raw: bytes) -> list[Term]:
    """ДРЛЗ export: `;`-separated, **Windows-1251** — decode explicitly
    (unit-tested against a known-mojibake fixture). Emits drug + form + dose
    terms for the 'plan' section templates."""
    text = raw.decode("windows-1251")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    if reader.fieldnames is None:
        return []
    fields = {f.strip().lower(): f for f in reader.fieldnames}
    name_col = next(
        (fields[k] for k in ("торговельна назва", "торгівельна назва", "назва", "trade_name") if k in fields),
        None,
    )
    form_col = next((fields[k] for k in ("форма випуску", "лікарська форма", "form") if k in fields), None)
    if name_col is None:
        raise ValueError("ДРЛЗ CSV: no trade-name column found")

    terms: list[Term] = []
    seen: set[str] = set()
    for row in reader:
        name = (row.get(name_col) or "").strip()
        if not name:
            continue
        form = (row.get(form_col) or "").strip() if form_col else ""
        for text_term in filter(None, (name, f"{name} {form}".strip() if form else "")):
            key = text_term.lower()
            if key not in seen:
                seen.add(key)
                terms.append(Term(text=text_term, section="plan"))
    return terms


def parse_term_lines(raw: bytes, *, section: str, encoding: str = "utf-8") -> list[Term]:
    """Formulary / НК 025 exports normalised to one term per line upstream
    ('#' comments allowed). НК 025 lines may carry a leading ICD code —
    kept: the code is part of how a diagnosis is dictated."""
    terms: list[Term] = []
    seen: set[str] = set()
    for line in raw.decode(encoding).splitlines():
        term = line.split("#", 1)[0].strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(Term(text=term, section=section))
    return terms


def load_dataset(path: Path) -> bytes:
    return path.read_bytes()
