"""Quota matrix (plan §6): a release fails if any declared cell lands
outside [lower, upper] of its target. Without this the corpus becomes 8k
cardiology-examination phrases and nothing in plan/follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from corpus_forge.domain.normalize import tokenize

SHORT_MAX_TOKENS = 5  # short = 3-5 tokens, long = 6-12 (quota.yaml header)


@dataclass(frozen=True, slots=True)
class QuotaCell:
    language: str
    specialty: str
    section: str
    bucket: str  # 'short' | 'long'
    target: int


@dataclass(frozen=True, slots=True)
class QuotaConfig:
    lower: float
    upper: float
    cells: tuple[QuotaCell, ...]


@dataclass(frozen=True, slots=True)
class QuotaViolation:
    cell: QuotaCell
    actual: int
    bound: str  # 'under' | 'over'


def load_quota(path: Path) -> QuotaConfig:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    tolerance = doc["tolerance"]
    cells = tuple(
        QuotaCell(
            language=str(c["language"]),
            specialty=str(c["specialty"]),
            section=str(c["section"]),
            bucket=str(c["bucket"]),
            target=int(c["target"]),
        )
        for c in doc["cells"]
    )
    return QuotaConfig(lower=float(tolerance["lower"]), upper=float(tolerance["upper"]), cells=cells)


def length_bucket(phrase: str) -> str:
    return "short" if len(tokenize(phrase)) <= SHORT_MAX_TOKENS else "long"


def check_quota(
    config: QuotaConfig,
    rows: list[tuple[str, str, str | None, str | None]],
) -> list[QuotaViolation]:
    """rows: (phrase, language, specialty, section_hint). Only declared cells
    are judged — vocabulary outside the matrix is allowed, not required."""
    counts: dict[tuple[str, str, str, str], int] = {}
    for phrase, language, specialty, section in rows:
        key = (language, specialty or "general", section or "", length_bucket(phrase))
        counts[key] = counts.get(key, 0) + 1

    violations: list[QuotaViolation] = []
    for cell in config.cells:
        actual = counts.get((cell.language, cell.specialty, cell.section, cell.bucket), 0)
        if actual < cell.target * config.lower:
            violations.append(QuotaViolation(cell=cell, actual=actual, bound="under"))
        elif actual > cell.target * config.upper:
            violations.append(QuotaViolation(cell=cell, actual=actual, bound="over"))
    return violations
