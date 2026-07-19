"""Template auto-selection for transcript-to-report assignment.

Given a raw transcript and the job language, pick the template the
dictation most likely follows. Deterministic keyword scoring — no model
call, no network: template *name* words weigh most, per-section
``voice_aliases`` next, section names least. Ukrainian inflection is
handled with a crude but effective prefix truncation («Рентгенографія»
matches «рентгенографії»).

The caller falls back to the tenant's general-medicine default when no
template clears the score threshold, and always reports WHICH mode
picked the template («explicit» | «auto» | «fallback») so the UI can
surface low-confidence picks for review.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Final
from uuid import UUID

import asyncpg

from template_models import TemplateDefinition

logger = logging.getLogger(__name__)

# A template must accumulate at least this score to win auto-selection.
MIN_AUTO_SCORE: Final = 3

_WORD_RE = re.compile(r"[a-zа-яіїєґ0-9']+", re.IGNORECASE)

_WEIGHT_NAME: Final = 3
_WEIGHT_ALIAS: Final = 2
_WEIGHT_SECTION: Final = 1
# Cap per-keyword hits so one repeated word can't dominate the score.
_MAX_HITS_PER_KEYWORD: Final = 3

# Fallback preference when nothing scores: the general-medicine template.
FALLBACK_SPECIALTY: Final = "internal_medicine"


@dataclass(frozen=True, slots=True)
class TemplateCandidate:
    id: UUID
    code: str
    name: str
    specialty: str
    schema_version: int
    definition: TemplateDefinition
    is_system: bool


@dataclass(frozen=True, slots=True)
class TemplateChoice:
    candidate: TemplateCandidate
    mode: str  # "auto" | "fallback"
    score: int


def _stem(token: str) -> str:
    """Prefix-truncate to survive Ukrainian case inflection.

    «рентгенографія» → «рентгенографі» matches «рентгенографії»;
    short tokens are kept whole (truncating them would over-match).
    """
    t = token.lower()
    return t[:-2] if len(t) >= 6 else t


def _keywords(text: str, *, min_len: int) -> set[str]:
    return {_stem(w) for w in _WORD_RE.findall(text.lower()) if len(w) >= min_len}


def score_candidate(candidate: TemplateCandidate, transcript_lower: str) -> int:
    score = 0
    for kw in _keywords(candidate.name, min_len=4):
        score += min(transcript_lower.count(kw), _MAX_HITS_PER_KEYWORD) * _WEIGHT_NAME
    for section in candidate.definition.sections:
        for alias in section.voice_aliases:
            # Aliases are whole phrases; match them as substrings.
            if len(alias) >= 4 and alias in transcript_lower:
                score += _WEIGHT_ALIAS
        for kw in _keywords(section.name, min_len=5):
            score += min(transcript_lower.count(kw), _MAX_HITS_PER_KEYWORD) * _WEIGHT_SECTION
    return score


async def load_candidates(
    conn: asyncpg.Connection, *, language: str
) -> list[TemplateCandidate]:
    """Active templates (system + this tenant's, via RLS) with parsed schema.

    Rows whose schema fails to parse are skipped with a warning — one
    malformed tenant template must not break assignment.
    """
    rows = await conn.fetch(
        """
        SELECT id, code, name, specialty, schema_version, schema_jsonb, is_system
        FROM templates
        WHERE language = $1 AND status = 'active'
        ORDER BY is_system, name, id
        """,
        language,
    )
    out: list[TemplateCandidate] = []
    # The dev seed is not idempotent for SYSTEM templates (tenant_id IS
    # NULL escapes the (tenant_id, code, schema_version) UNIQUE — NULLs
    # compare distinct), so identical rows pile up. Keep the first id per
    # (code, schema_version) — rows are byte-identical duplicates.
    seen: set[tuple[str, int]] = set()
    for row in rows:
        dedupe_key = (str(row["code"]), int(row["schema_version"]))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        raw = row["schema_jsonb"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        try:
            definition = TemplateDefinition.model_validate(raw)
        except Exception:  # noqa: BLE001 — tolerate one bad row
            logger.warning("template_match.schema_invalid", extra={"template_id": str(row["id"])})
            continue
        out.append(
            TemplateCandidate(
                id=row["id"],
                code=str(row["code"]),
                name=row["name"],
                specialty=row["specialty"],
                schema_version=int(row["schema_version"]),
                definition=definition,
                is_system=bool(row["is_system"]),
            )
        )
    return out


def select_template(
    candidates: list[TemplateCandidate], transcript: str
) -> TemplateChoice | None:
    """Best-scoring candidate, or the general-medicine fallback.

    Returns ``None`` only when the catalogue is empty. Ties break on
    (tenant template over system, name) for determinism.
    """
    if not candidates:
        return None
    transcript_lower = transcript.lower()
    scored = sorted(
        ((score_candidate(c, transcript_lower), c) for c in candidates),
        key=lambda pair: (-pair[0], pair[1].is_system, pair[1].name),
    )
    best_score, best = scored[0]
    if best_score >= MIN_AUTO_SCORE:
        return TemplateChoice(candidate=best, mode="auto", score=best_score)
    # Fallback order: the general-visit template ("internal_medicine*"
    # code), then any general-medicine template, then whatever exists.
    fallback = next(
        (c for c in candidates if c.code.startswith("internal_medicine")),
        next(
            (c for c in candidates if c.specialty == FALLBACK_SPECIALTY),
            candidates[0],
        ),
    )
    return TemplateChoice(candidate=fallback, mode="fallback", score=best_score)
