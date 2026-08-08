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
            self.entries,
            key=lambda e: 0 if e.is_tenant_override else 1,
        )


@dataclass(frozen=True, slots=True)
class ChoiceOption:
    """One selectable option of a choice/multi_choice section (sprint 13).

    Mirrors ``template_models.ChoiceOption`` on the wire. ``aliases``
    arrive already normalized (NFC, lower-case, stripped) because the
    template model normalizes them at validation — the extractor never
    re-normalizes template data.
    """

    value: str
    label: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TemplateSection:
    """One section a `section.<name>` voice command can navigate to.

    Sprint-13 additions (``section_key``/``field_type``/``options``) are
    optional: a caller that only navigates sections keeps sending
    id/name/aliases and the field-extraction stage stays inert for it.
    """

    id: UUID
    name: str
    aliases: tuple[str, ...] = ()
    # Sprint 13 — the template's section slug (``TemplateSection.id`` in
    # template_models). ``id`` here is the template row UUID, which is
    # NOT what report content keys sections by.
    section_key: str = ""
    field_type: str = "free_text"
    options: tuple[ChoiceOption, ...] = ()


@dataclass(frozen=True, slots=True)
class ProcessingContext:
    """Per-request immutable context. Stages MUST NOT mutate this."""

    tenant_id: UUID
    language: Literal["uk", "en", "de"]
    specialty: str | None
    reference_date: date
    is_partial: bool
    abbreviation_snapshot: AbbreviationSnapshot
    pipeline_version: str
    template_sections: tuple[TemplateSection, ...] = ()
    decimal_separator: str = ","
    bp_separator: str = "/"
    date_format: Literal["DD.MM.YYYY", "YYYY-MM-DD", "WORD"] = "DD.MM.YYYY"
    # Batch path only: there is no editor to consume Operations, so
    # text-shaped ops (insert_punctuation / line breaks) are applied
    # into ``text`` at the command's position by Stage 1. Streaming
    # keeps False — the FE editor owns op application (sprint 04/06).
    apply_operations_inline: bool = False
    # Sprint 14: stage names (matching ``Stage.name``) the orchestrator
    # must skip for this request — conversation-mode transcripts pass
    # ("voice_commands",) so patient speech can never trigger editing
    # operations. Callers normalize (dedupe + sort) before constructing;
    # the value participates in the idempotence cache key.
    stages_disabled: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NumericArtifact:
    """One measurement the number normalizer actually produced.

    Sprint 13: the field-extraction binder consumes these instead of
    re-reading the text. Spoken-numeral logic ("сто сорок" → 140) lives
    in exactly one place — the normalizer — and a binder that re-derived
    it would drift the moment the normalizer changed. ``rendered`` is
    the exact substring written into the text, so a caller can locate
    it without guessing the per-request separators.
    """

    value: str  # normalized numeric form, e.g. "140" or "37,2"
    unit: str  # "" when the utterance carried no unit
    rendered: str  # exactly what was written into the text
    token_index: int  # position in the normalizer's output token stream


@dataclass(frozen=True, slots=True)
class DateArtifact:
    """One ISO date present in the date normalizer's output."""

    iso: str  # YYYY-MM-DD
    char_index: int  # position in the normalized text


@dataclass(frozen=True, slots=True)
class StageInput:
    """Input to a pipeline stage."""

    text: str
    words: tuple[Word, ...] = ()
    confidence_spans: tuple[ConfidenceSpan, ...] = ()
    voice_commands: tuple[CommandSlot, ...] = ()
    operations: tuple[Operation, ...] = ()
    warnings: tuple[PipelineWarning, ...] = ()
    # Sprint 13: structured products of EARLIER stages, threaded by the
    # orchestrator. Additive and defaulted, so every pre-S13 stage and
    # test constructing a StageInput is unaffected.
    numeric_artifacts: tuple[NumericArtifact, ...] = ()
    date_artifacts: tuple[DateArtifact, ...] = ()


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
    # Sprint 13: structured products a stage exposes to LATER stages.
    # Not serialized into the response body — see the orchestrator's
    # accumulation note.
    numeric_artifacts: tuple[NumericArtifact, ...] = ()
    date_artifacts: tuple[DateArtifact, ...] = ()

    def as_input(self) -> StageInput:
        return StageInput(
            text=self.text,
            words=self.words,
            confidence_spans=self.confidence_spans,
            voice_commands=self.voice_commands,
            operations=self.operations,
            warnings=self.warnings,
            numeric_artifacts=self.numeric_artifacts,
            date_artifacts=self.date_artifacts,
        )


class Stage(Protocol):
    """Sprint-05 pipeline stage Protocol."""

    name: str

    async def process(self, ctx: ProcessingContext, input: StageInput) -> StageOutput: ...

    @property
    def runs_on_partials(self) -> bool:
        """True if this stage runs on partials (sprint-05: only stages 1 + 6)."""
        ...
