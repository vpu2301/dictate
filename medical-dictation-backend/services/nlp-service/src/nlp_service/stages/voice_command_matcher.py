"""Voice command detector.

The matcher walks a word stream and emits :class:`CommandSlot` for
intentional voice commands. Three gates defend against false positives:

1. **Pause-before**: the silence between the previous content word and
   the first command word must exceed ``requires_pause_before_ms``.
2. **Confidence**: the average Whisper word-probability across the
   matched span must be ≥ ``min_avg_probability``.
3. **Edit-distance tolerance**: at most one substitution per phrase;
   substituted word's Levenshtein distance from expected ≤ 2.

False positives are the primary clinical-trust risk in sprint 5; the
defaults err on the side of "didn't fire" rather than "fired wrong."

Section commands (``intent`` starts with ``section.``) additionally
resolve their argument by matching the words AFTER the command head
against the active template's section names/aliases. If no match, the
command is rejected and the words are returned as content.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from ..pipeline.base import CommandSlot, TemplateSection, Word

logger = logging.getLogger(__name__)

_MAX_SUBSTITUTION_DISTANCE: Final = 2
_MAX_SUBSTITUTIONS_PER_PHRASE: Final = 1

# Whisper attaches sentence punctuation to word tokens («Крапка.», "period,").
# Strip token EDGES only before comparing against catalogue phrases, so the
# attached mark neither consumes the edit-distance budget nor blocks a match.
_EDGE_PUNCT: Final = ".,!?;:…«»„“”\"'()[]"


def _norm_token(text: str) -> str:
    return text.lower().strip().strip(_EDGE_PUNCT)


@dataclass(frozen=True, slots=True)
class _PhraseSpec:
    intent: str
    words: tuple[str, ...]
    requires_pause_before_ms: int
    min_avg_probability: float
    is_section_command: bool
    is_option_command: bool = False
    exact_match_only: bool = False


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One command in the catalogue. ``phrases`` is a list of word-tuples."""

    intent: str
    language: str
    phrases: tuple[tuple[str, ...], ...]
    requires_pause_before_ms: int = 200
    min_avg_probability: float = 0.85
    is_section_command: bool = False
    # Sprint 13: the phrase is followed by an OPTION name, resolved
    # against the template's choice sections (see _resolve_option).
    is_option_command: bool = False
    # Sprint 13: disable the 1-substitution edit-distance tolerance for
    # THIS spec. Required wherever a near-miss would trigger the OPPOSITE
    # action — "прибрати" (remove) is Levenshtein-2 from "обрати" (set),
    # so fuzzy heads would let "remove penicillin" select it instead.
    exact_match_only: bool = False


@dataclass(frozen=True, slots=True)
class MatchResult:
    """One detected match. ``after_index`` is the next word the caller
    should resume from."""

    slot: CommandSlot
    consumed_word_indices: tuple[int, ...]
    after_index: int
    # Other distinct intents that also matched this exact span — when
    # non-empty the match is ambiguous and the caller should warn rather
    # than silently committing to the (arbitrary longest-first) winner.
    ambiguous_with: tuple[str, ...] = ()


class VoiceCommandMatcher:
    """Build a flat list of phrase-specs sorted longest-first.

    For sprint-05 volumes (15+ commands × ~3 phrases each × ≤ 4 words)
    a naive scan is O(N · |catalogue|) and lands well under the 5 ms
    p95 budget on a 50-word segment. The trie optimisation lives in
    the spec but isn't worth the complexity yet — revisit if the
    catalogue grows past ~100 phrases.
    """

    def __init__(
        self,
        specs: list[CommandSpec],
        *,
        language: str,
        template_sections: tuple[TemplateSection, ...] = (),
    ) -> None:
        self._language = language
        self._sections = template_sections
        flat: list[_PhraseSpec] = []
        for spec in specs:
            if spec.language != language:
                continue
            for phrase in spec.phrases:
                flat.append(
                    _PhraseSpec(
                        intent=spec.intent,
                        words=tuple(p.lower() for p in phrase),
                        requires_pause_before_ms=spec.requires_pause_before_ms,
                        min_avg_probability=spec.min_avg_probability,
                        is_section_command=spec.is_section_command,
                        is_option_command=spec.is_option_command,
                        exact_match_only=spec.exact_match_only,
                    )
                )
        # Longest phrases first so "новий абзац" wins over "абзац".
        flat.sort(key=lambda p: -len(p.words))
        self._phrases = flat

    def detect(self, words: list[Word]) -> list[MatchResult]:
        """Return every non-overlapping command detected in ``words``."""
        results: list[MatchResult] = []
        i = 0
        while i < len(words):
            match = self._try_match_at(words, i)
            if match is None:
                i += 1
                continue
            results.append(match)
            i = match.after_index
        return results

    def _try_match_at(self, words: list[Word], i: int) -> MatchResult | None:
        for phrase in self._phrases:
            n = len(phrase.words)
            if not self._passes_gates(words, i, phrase):
                continue
            window = words[i : i + n]
            avg_p = sum(w.probability for w in window) / n if n else 0.0

            # Ambiguity: a different intent of the SAME length that also
            # clears every gate at this position. The longest-first winner
            # is otherwise arbitrary, so surface the collision.
            ambiguous_with = tuple(
                sorted(
                    {
                        other.intent
                        for other in self._phrases
                        if len(other.words) == n
                        and other.intent != phrase.intent
                        and self._passes_gates(words, i, other)
                    }
                )
            )

            consumed = tuple(range(i, i + n))
            after = i + n
            arg: dict[str, str] | None = None

            if phrase.is_option_command:
                resolved, option_consumed = self._resolve_option(words, i + n, intent=phrase.intent)
                if resolved is None:
                    # The head matched but no option followed at all —
                    # reject so the words stay content ("обрати" alone is
                    # ordinary Ukrainian, not a command).
                    continue
                arg = dict(resolved)
                consumed = consumed + tuple(range(i + n, i + n + option_consumed))
                after = i + n + option_consumed
                intent_full = phrase.intent
            elif phrase.is_section_command:
                section_id, section_consumed = self._resolve_section(words, i + n)
                if section_id is None:
                    # Section command needs an argument; without one we
                    # reject so the words become content.
                    continue
                arg = {"section_id": str(section_id)}
                consumed = consumed + tuple(range(i + n, i + n + section_consumed))
                after = i + n + section_consumed
                intent_full = f"section.{section_id}"
            elif phrase.intent == "diagnosis.capture":
                # A HINT, not a selection: mark where the dictated
                # diagnosis text begins so step-05's extractor (and the
                # FE) know what to read. No ICD-10 is chosen by voice.
                arg = {"from_word_index": str(after)}
                intent_full = phrase.intent
            else:
                intent_full = phrase.intent

            slot = CommandSlot(
                intent=intent_full,
                span_start_s=window[0].start_s,
                span_end_s=words[after - 1].end_s,
                confidence=avg_p,
                arg=arg,
            )
            return MatchResult(
                slot=slot,
                consumed_word_indices=consumed,
                after_index=after,
                ambiguous_with=ambiguous_with,
            )
        return None

    # ── Gates ───────────────────────────────────────────────────────

    def _passes_gates(self, words: list[Word], i: int, phrase: _PhraseSpec) -> bool:
        """All three FP gates (length, edit-distance, pause, confidence)."""
        n = len(phrase.words)
        if i + n > len(words):
            return False
        if phrase.exact_match_only:
            if not self._matches_exactly(words, i, phrase.words):
                return False
        elif not self._matches_with_edit_distance(words, i, phrase.words):
            return False
        if not self._pause_ok(words, i, phrase.requires_pause_before_ms):
            return False
        window = words[i : i + n]
        avg_p = sum(w.probability for w in window) / n if n else 0.0
        return avg_p >= phrase.min_avg_probability

    def _matches_exactly(self, words: list[Word], i: int, expected: tuple[str, ...]) -> bool:
        """Zero tolerance. Used by specs where a near-miss would fire the
        wrong — sometimes opposite — action."""
        return all(_norm_token(words[i + j].text) == target for j, target in enumerate(expected))

    def _matches_with_edit_distance(
        self, words: list[Word], i: int, expected: tuple[str, ...]
    ) -> bool:
        substitutions = 0
        for j, target in enumerate(expected):
            actual = _norm_token(words[i + j].text)
            if actual == target:
                continue
            d = _levenshtein(actual, target)
            if d > _MAX_SUBSTITUTION_DISTANCE:
                return False
            substitutions += 1
            if substitutions > _MAX_SUBSTITUTIONS_PER_PHRASE:
                return False
        return True

    def _pause_ok(self, words: list[Word], i: int, required_ms: int) -> bool:
        if i == 0:
            return True  # nothing before — no pause requirement
        prev = words[i - 1]
        curr = words[i]
        observed_ms = max(0.0, curr.start_s - prev.end_s) * 1000.0
        return observed_ms >= required_ms

    # ── Section argument resolution ────────────────────────────────

    def _resolve_section(self, words: list[Word], start: int) -> tuple[str | None, int]:
        """Look at the next 1–3 words; return (section_id, words_consumed).

        Sections are matched by name OR alias, case-insensitively. If
        no match found in the first 3 tokens, return (None, 0).
        """
        if start >= len(words):
            return None, 0
        if not self._sections:
            return None, 0
        for span in (3, 2, 1):
            if start + span > len(words):
                continue
            candidate = " ".join(_norm_token(w.text) for w in words[start : start + span])
            for section in self._sections:
                names = [section.name.lower(), *(a.lower() for a in section.aliases)]
                if candidate in names:
                    return str(section.id), span
        return None, 0

    # ── Option argument resolution (sprint 13) ─────────────────────

    def _resolve_option(
        self, words: list[Word], start: int, *, intent: str
    ) -> tuple[dict[str, str] | None, int]:
        """Resolve the option name after a choice command.

        Returns ``({section_key, value} | {reason}, words_consumed)``.

        The FSM layer matches EXACTLY (normalized), never fuzzily:
        fuzziness belongs to the extractor, where a wrong guess only
        produces a proposal the clinician can reject. A voice command
        writes directly, so an unresolvable option must become a
        precise no-op — never a guess.

        As-built note: ``ProcessingContext`` has no "active section"
        (verified in sprint 13 step 04 — section-aware streaming was
        never wired). So the option resolves across ALL choice sections
        in the template snapshot, and an alias claimed by two sections
        is reported ambiguous rather than arbitrarily assigned.
        """
        if start >= len(words) or not self._sections:
            return None, 0

        choice_sections = [s for s in self._sections if s.field_type in ("choice", "multi_choice")]
        if not choice_sections:
            # No typed sections at all — "обрати" here is ordinary
            # Ukrainian. Reject so the word stays in the note.
            return None, 0

        for span in (4, 3, 2, 1):
            if start + span > len(words):
                continue
            candidate = " ".join(_norm_token(w.text) for w in words[start : start + span])
            if not candidate:
                continue
            hits: list[tuple[str, str, str]] = []
            for section in choice_sections:
                for option in section.options:
                    names = {option.label.lower(), *option.aliases}
                    if candidate in names:
                        key = section.section_key or str(section.id)
                        hits.append((key, option.value, section.field_type))
            unique = sorted({(k, v) for k, v, _ in hits})
            if len(unique) == 1:
                section_key, value = unique[0]
                field_type = next(ft for k, v, ft in hits if (k, v) == unique[0])
                # Commands mirror field semantics STRICTLY — predictability
                # over cleverness. add/remove are meaningless on a
                # single-select field, so they no-op with a precise reason
                # instead of being silently reinterpreted as "set".
                if intent in ("choice.add", "choice.remove") and field_type == "choice":
                    return {
                        "reason": "not_a_multi_choice_section",
                        "section_key": section_key,
                    }, span
                return {"section_key": section_key, "value": value}, span
            if len(unique) > 1:
                # The same words name options in two sections — the
                # clinician must disambiguate; we never pick.
                return {"reason": "option_ambiguous"}, span
        # Words followed the head but named no option we know. REJECT
        # rather than emitting an "option_not_found" no-op: a no-op would
        # still CONSUME the head, deleting a word from clinical prose
        # ("встановити діагноз поки неможливо"). A missing toast is
        # recoverable; a silently eaten clinical word is not. A reason is
        # only reported when an option name was positively recognised
        # (ambiguous, or right name / wrong field type).
        return None, 0


def _levenshtein(a: str, b: str) -> int:
    """Plain edit-distance. Cap at MAX_SUBSTITUTION_DISTANCE+1 for early-exit."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if abs(len(a) - len(b)) > _MAX_SUBSTITUTION_DISTANCE:
        return _MAX_SUBSTITUTION_DISTANCE + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]
