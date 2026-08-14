"""Phrase-ification — terms are not phrases (sprint plan §3).

A term (`Гіпертонічна хвороба`, `Бісопролол 5 мг`) plus a section maps to
the 2-5 ways a clinician actually dictates it, via a small hand-written
template set per (language, section) — not per term. Templates live in
infra/seeds/corpus/phrasing/<lang>/<section>.yaml, are reviewed once, and
apply to thousands of terms. Review the template, not the output.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from corpus_forge.domain.ngram import MAX_CHARS


@dataclass(frozen=True, slots=True)
class PhrasingTemplateSet:
    language: str
    section: str
    templates: tuple[str, ...]  # each contains exactly one '{term}' slot


def load_template_set(path: Path) -> PhrasingTemplateSet:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a mapping")
    templates = doc.get("templates")
    if not isinstance(templates, list) or not templates:
        raise ValueError(f"{path}: 'templates' must be a non-empty list")
    for template in templates:
        if not isinstance(template, str) or template.count("{term}") != 1:
            raise ValueError(f"{path}: every template needs exactly one {{term}}: {template!r}")
    return PhrasingTemplateSet(
        language=str(doc["language"]),
        section=str(doc["section"]),
        templates=tuple(templates),
    )


def load_all_template_sets(root: Path) -> list[PhrasingTemplateSet]:
    return [load_template_set(p) for p in sorted(root.glob("*/*.yaml"))]


def phrasify(term: str, template_set: PhrasingTemplateSet) -> list[str]:
    """Apply every template; drop results that overflow the 80-char limit
    (long compound drug names overflow decorated templates — the bare-term
    template still carries them)."""
    term = term.strip()
    if not term:
        return []
    out: list[str] = []
    for template in template_set.templates:
        phrase = template.format(term=term)
        if 1 <= len(phrase) <= MAX_CHARS and phrase not in out:
            out.append(phrase)
    return out
