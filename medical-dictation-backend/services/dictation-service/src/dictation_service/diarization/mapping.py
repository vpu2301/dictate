"""Doctor/patient mapping inference (sprint 14, ADR-0034).

Heuristic, explainable, conservative — per the sprint contract there is
no black-box classifier in the pilot. Two signals:

  1. **Opener** — which speaker owns the majority of the first sustained
     stretch of speech (consultations usually open with the clinician,
     but this signal alone is weak: fixture ``uk-patient-opens-003``
     exists precisely because patients open sessions too).
  2. **Medical-register density** — clinician speech acts (imperatives
     of the consult, prescriptions, orders for tests) per word, matched
     against a curated prefix lexicon per language.

Evidence per speaker  = 0.35·opener + 0.65·vocab share.
Hypothesis confidence = 0.5 + 0.45·|evidence gap|   (bounded 0.5–0.95).
A hint is only emitted at ≥ ``emit_threshold``; the hypothesis flips
only on strong evidence (new confidence ≥ old + ``flip_margin``); and a
manual ``SetSpeakerMapping`` freezes it permanently.

**The opener alone is never enough.** Without at least
``min_lexicon_hits`` clinician-register matches, the vocabulary signal
carries no information and the opener prior would decide the mapping on
its own — a coin flip dressed up as a confident claim. Consultations
where the patient speaks first are common (fixture
``uk-patient-opens-003``), and degraded ASR silently erases the
vocabulary signal, so this abstention is what keeps the pilot honest:
propose nothing, let the clinician assign it in one tap
(``SetSpeakerMapping``), and never mislabel the record.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .stream import SpeakerSegment

UNKNOWN = "UNKNOWN"

# Clinician-register lexicons. Entries ending in "*" are prefix matches
# (Ukrainian inflection); bare entries match the whole token.
_DOCTOR_LEXICON: dict[str, tuple[str, ...]] = {
    "uk": (
        "призна*", "направ*", "рекоменд*", "обстеж*", "аналіз*", "діагноз*",
        "анамнез*", "терапі*", "моніторуван*", "електрокардіограм*",
        "ехокардіограф*", "тропонін*", "мигдалик*", "пальпаці*", "аускульта*",
        "проходьте", "сідайте", "розкажіть", "розповідайте", "скажіть",
        "відкрийте", "здайте", "приймаєте", "вимірювали", "скаржитеся",
        "переливання", "чи",
    ),
    "en": (
        "prescrib*", "order*", "recommend*", "diagnos*", "mri", "x-ray",
        "lumbar", "anti-inflammatory", "radiate*", "numbness", "bladder",
        "seat", "brings",
    ),
    # German: prefix entries cover the inflected verb forms a clinician
    # actually uses in the consult ("verschreibe/verschreiben/verschrieben")
    # and the separable-prefix nouns ("Überweisung", "Untersuchung").
    "de": (
        "verschreib*", "verordn*", "überweis*", "untersuch*", "empfehl*",
        "diagnos*", "anamnes*", "befund*", "therapi*", "abhör*",
        "abtast*", "palpat*", "auskultat*", "blutbild", "blutdruck",
        "ekg", "röntgen*", "ultraschall", "labor*", "rezept*",
        "nehmen", "setzen", "erzählen", "beschreiben", "atmen",
        "beschwerden", "seit",
    ),
}


def _tokens(text: str) -> list[str]:
    return [t.strip(".,;:!?«»()\"'—-").lower() for t in text.split() if t.strip()]


def _lexicon_hits(tokens: list[str], language: str) -> int:
    lex = _DOCTOR_LEXICON.get(language, ())
    hits = 0
    for tok in tokens:
        for entry in lex:
            if entry.endswith("*"):
                if tok.startswith(entry[:-1]):
                    hits += 1
                    break
            elif tok == entry:
                hits += 1
                break
    return hits


@dataclass(frozen=True)
class MappingHypothesis:
    mapping: dict[str, str]  # e.g. {"S1": "doctor", "S2": "patient"}
    confidence: float
    rationale: str


@dataclass
class SpeakerMappingInference:
    language: str
    opener_window_ms: int = 10_000
    min_words_per_speaker: int = 6
    emit_threshold: float = 0.60
    flip_margin: float = 0.15
    # Minimum clinician-register matches (summed over speakers) before a
    # doctor/patient claim is made at all. Below this the vocabulary
    # signal is noise and only the opener prior would be talking.
    min_lexicon_hits: int = 2

    frozen: bool = False
    frozen_mapping: dict[str, str] | None = None
    _words: dict[str, list[str]] = field(default_factory=dict)
    _opener_ms: dict[str, int] = field(default_factory=dict)
    _opener_done: bool = False
    _last_emitted: MappingHypothesis | None = None

    # ── evidence feeds ────────────────────────────────────────────────
    def observe_word(self, text: str, speaker: str | None) -> None:
        if self.frozen or speaker in (None, UNKNOWN):
            return
        assert speaker is not None
        self._words.setdefault(speaker, []).extend(_tokens(text))

    def observe_segments(self, segments: list[SpeakerSegment]) -> None:
        if self.frozen or self._opener_done:
            return
        for seg in segments:
            if seg.start_ms >= self.opener_window_ms:
                self._opener_done = True
                break
            if seg.label == UNKNOWN:
                continue
            span = min(seg.end_ms, self.opener_window_ms) - seg.start_ms
            self._opener_ms[seg.label] = self._opener_ms.get(seg.label, 0) + max(0, span)

    # ── manual override ───────────────────────────────────────────────
    def freeze(self, mapping: dict[str, str]) -> None:
        """Clinician's manual assignment: authoritative, permanent."""
        self.frozen = True
        self.frozen_mapping = dict(mapping)

    # ── hypothesis ────────────────────────────────────────────────────
    def evaluate(self) -> MappingHypothesis | None:
        """Recompute; return a NEW hypothesis worth emitting (first hint,
        or a strong-evidence flip), else None. Never returns after
        ``freeze()``."""
        if self.frozen:
            return None
        speakers = sorted(set(self._words) | set(self._opener_ms))
        if len(speakers) < 2:
            return None
        s1, s2 = speakers[0], speakers[1]
        w1, w2 = self._words.get(s1, []), self._words.get(s2, [])
        if len(w1) < self.min_words_per_speaker or len(w2) < self.min_words_per_speaker:
            return None

        opener_total = sum(self._opener_ms.values())
        opener = {s: (self._opener_ms.get(s, 0) / opener_total if opener_total else 0.5) for s in (s1, s2)}
        h1, h2 = _lexicon_hits(w1, self.language), _lexicon_hits(w2, self.language)
        if (h1 + h2) < self.min_lexicon_hits or h1 == h2:
            # No discriminating vocabulary evidence — abstain rather than
            # let the opener prior decide (see module docstring).
            return None
        d1, d2 = h1 / max(1, len(w1)), h2 / max(1, len(w2))
        vocab_total = d1 + d2
        vocab = {s1: (d1 / vocab_total if vocab_total else 0.5), s2: (d2 / vocab_total if vocab_total else 0.5)}

        evidence = {s: 0.35 * opener[s] + 0.65 * vocab[s] for s in (s1, s2)}
        doctor = max(evidence, key=lambda s: evidence[s])
        patient = s2 if doctor == s1 else s1
        gap = abs(evidence[s1] - evidence[s2]) * 2  # gap of shares ∈ [0,1]
        confidence = round(min(0.95, 0.5 + 0.45 * min(1.0, gap)), 4)
        rationale = (
            f"opener {opener[doctor]:.2f} vs {opener[patient]:.2f}; "
            f"clinician-register density {vocab[doctor]:.2f} vs {vocab[patient]:.2f}"
        )
        hypothesis = MappingHypothesis(
            mapping={doctor: "doctor", patient: "patient"},
            confidence=confidence,
            rationale=rationale,
        )

        if confidence < self.emit_threshold:
            return None
        if self._last_emitted is None:
            self._last_emitted = hypothesis
            return hypothesis
        if hypothesis.mapping == self._last_emitted.mapping:
            # Same belief, refreshed confidence — not worth a message.
            self._last_emitted = hypothesis
            return None
        # A flip: demand strong evidence beyond the previously stated belief.
        if confidence >= self._last_emitted.confidence + self.flip_margin:
            self._last_emitted = hypothesis
            return hypothesis
        return None

    @property
    def current(self) -> MappingHypothesis | None:
        if self.frozen and self.frozen_mapping is not None:
            return MappingHypothesis(dict(self.frozen_mapping), 1.0, "manual override")
        return self._last_emitted
