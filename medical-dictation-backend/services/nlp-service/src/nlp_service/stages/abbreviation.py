"""Stage 5 — abbreviation policy (per-tenant + global merge).

The snapshot pattern is the key abstraction: ``AbbreviationSnapshot`` is
read ONCE at request entry (see ``main_deps.fetch_abbreviation_snapshot``),
passed through ``ProcessingContext``, and re-used by every cached-key
computation. Admin edits in-flight don't affect the current request.

Direction:
- ``compact`` (default): write the abbreviation form (replace expanded).
- ``expand``: write the expanded form (replace abbreviation).
- ``either``: pass through.

Tenant override beats global on the same ``(language, expanded, abbreviated)``.
Domain filter prefers entries whose ``domain`` matches ``ctx.specialty``,
falling back to ``domain='all'``, then to NULL.

Word-boundary matching is mandatory — never substitute "ІМ" inside
"імпорт".
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from ..pipeline.base import (
    AbbreviationEntry,
    ProcessingContext,
    Stage,
    StageInput,
    StageOutput,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CompiledRule:
    pattern: re.Pattern[str]
    replacement: str
    domain: str | None


class AbbreviationStage:
    """Sprint-05 Stage 5."""

    name = "abbreviation"
    runs_on_partials: bool = False

    async def process(
        self, ctx: ProcessingContext, input: StageInput
    ) -> StageOutput:
        t0 = time.monotonic()
        rules = _compile_rules(ctx)
        new_text = input.text
        applied = 0
        for rule in rules:
            new_text, n = rule.pattern.subn(rule.replacement, new_text)
            applied += n
        return StageOutput(
            text=new_text,
            words=input.words,
            confidence_spans=input.confidence_spans,
            voice_commands=input.voice_commands,
            operations=input.operations,
            warnings=input.warnings,
            metadata={
                self.name + ".latency_ms": (time.monotonic() - t0) * 1000.0,
                self.name + ".applied": applied,
                self.name + ".snapshot_fingerprint": ctx.abbreviation_snapshot.fingerprint,
            },
        )


def _compile_rules(ctx: ProcessingContext) -> list[_CompiledRule]:
    """Project the snapshot into a list of compiled regex rules.

    Order matters: tenant overrides FIRST so a global rule never wins
    against a tenant one. Domain-matching rules win over ``all`` which
    wins over NULL domain.
    """
    relevant: list[AbbreviationEntry] = []
    seen: set[tuple[str, str]] = set()
    sorted_entries = sorted(
        ctx.abbreviation_snapshot.entries,
        key=lambda e: (
            0 if e.is_tenant_override else 1,
            0 if e.domain == ctx.specialty else (1 if e.domain == "all" else 2),
        ),
    )
    for e in sorted_entries:
        key = (e.expanded.lower(), e.abbreviated.lower())
        if key in seen:
            continue
        seen.add(key)
        relevant.append(e)

    rules: list[_CompiledRule] = []
    for e in relevant:
        if e.direction == "either":
            continue
        if e.direction == "compact":
            src, dst = e.expanded, e.abbreviated
        else:  # expand
            src, dst = e.abbreviated, e.expanded
        flags = 0 if e.case_sensitive else re.IGNORECASE
        # Word-boundary on both sides; the Unicode flag matters for Cyrillic.
        pattern = re.compile(
            r"(?<![\wА-ЯЁІЇЄҐа-яёіїєґ])"
            + re.escape(src)
            + r"(?![\wА-ЯЁІЇЄҐа-яёіїєґ])",
            flags | re.UNICODE,
        )
        rules.append(_CompiledRule(pattern=pattern, replacement=dst, domain=e.domain))
    return rules
