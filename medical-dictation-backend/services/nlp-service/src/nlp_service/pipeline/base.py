"""Stage interface + pipeline types.

The 6-stage NLP pipeline (voice_commands → punctuation → number_norm →
date_norm → abbreviation → confidence) communicates via these immutable
records. Every stage takes the previous stage's output and returns a
new ``StageOutput``; the orchestrator threads them.

Why discriminated-union messages instead of mutating dicts: the
pipeline runs against PHI-bearing text in production, and silent
in-place mutation makes idempotence regressions invisible. Frozen
dataclasses force every stage to be explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Word:
    """One word + word-level metadata.

    Carries through from Whisper (sprint 03/04) — ``probability`` is
    the model's per-word confidence. ``is_voice_command_token`` is set
    by Stage 1 (voice_commands); it lets downstream stages skip
    command tokens during punctuation/number normalization.
    """

    text: str
    start_s: float
    end_s: float
    probability: float
    is_voice_command_token: bool = False


@dataclass(frozen=True, slots=True)
class ConfidenceSpan:
    """A character range in the post-processed text with a confidence label."""

    start_char: int
    end_char: int
    level: Literal["high_concern", "moderate"]


@dataclass(frozen=True, slots=True)
class CommandSlot:
    """One detected voice command."""

    intent: str
    span_start_s: float
    span_end_s: float
    confidence: float
    arg: dict[str, str] | None = None  # e.g., {"section_id": "..."}


@dataclass(frozen=True, slots=True)
class Operation:
    """A frontend-actionable operation, derived from a CommandSlot.

    The frontend executes these to mutate the editor state.
    """

    op: str
    arg: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class PipelineWarning:
    code: str
    detail: str = ""
    stage: str = ""


@dataclass(frozen=True, slots=True)
class AbbreviationEntry:
    """One row from ``abbreviation_dictionary``, snapshotted at request entry."""

    expanded: str
    abbreviated: str
    direction: Literal["expand", "compact", "either"]
    domain: str | None
    case_sensitive: bool
    is_tenant_override: bool


@dataclass(frozen=True, slots=True)
class AbbreviationSnapshot:
    """Immutable per-request view of the merged abbreviation dictionary.

    Sprint-05 contract: the snapshot is taken at request entry; in-flight
    requests don't observe admin edits. The ``fingerprint`` field is a
    stable hash of the snapshot; the idempotence key includes it.
    """

    entries: tuple[AbbreviationEntry, ...]
    fingerprint: str

    def for_language(self, language: str) -> list[AbbreviationEntry]:
        # Tenant overrides FIRST, so the matcher's first-match wins.
        return sorted(
            [e for e in self.entries],
            key=lambda e: (0 if e.is_tenant_override else 1),
        )


@dataclass(frozen=True, slots=True)
class TemplateSection:
    """One section a `section.<name>` voice command can navigate to."""

    id: UUID
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProcessingContext:
    """Per-request immutable context. Stages MUST NOT mutate this."""

    tenant_id: UUID
    language: Literal["uk", "en"]
    specialty: str | None
    reference_date: date
    is_partial: bool
    abbreviation_snapshot: AbbreviationSnapshot
    pipeline_version: str
    template_sections: tuple[TemplateSection, ...] = ()
    decimal_separator: str = ","
    bp_separator: str = "/"
    date_format: Literal["DD.MM.YYYY", "YYYY-MM-DD", "WORD"] = "DD.MM.YYYY"


@dataclass(frozen=True, slots=True)
class StageInput:
    """Input to a pipeline stage."""

    text: str
    words: tuple[Word, ...] = ()
    confidence_spans: tuple[ConfidenceSpan, ...] = ()
    voice_commands: tuple[CommandSlot, ...] = ()
    operations: tuple[Operation, ...] = ()
    warnings: tuple[PipelineWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class StageOutput:
    """Output of a pipeline stage. Carries per-stage telemetry in ``metadata``."""

    text: str
    words: tuple[Word, ...] = ()
    confidence_spans: tuple[ConfidenceSpan, ...] = ()
    voice_commands: tuple[CommandSlot, ...] = ()
    operations: tuple[Operation, ...] = ()
    warnings: tuple[PipelineWarning, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_input(self) -> StageInput:
        return StageInput(
            text=self.text,
            words=self.words,
            confidence_spans=self.confidence_spans,
            voice_commands=self.voice_commands,
            operations=self.operations,
            warnings=self.warnings,
        )


class Stage(Protocol):
    """Sprint-05 pipeline stage Protocol."""

    name: str

    async def process(
        self, ctx: ProcessingContext, input: StageInput
    ) -> StageOutput: ...

    @property
    def runs_on_partials(self) -> bool:
        """True if this stage runs on partials (sprint-05: only stages 1 + 6)."""
        ...
