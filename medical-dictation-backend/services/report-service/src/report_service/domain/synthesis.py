"""Report synthesis seam (spec item 1).

A :class:`Synthesizer` turns the *raw dictation* of a single report
section into a lightly-cleaned, presentation-ready string. The full
production-shaped contract is wired here, but the default implementation
is a **deterministic mock** — no external LLM call, no ``anthropic``
dependency. Swapping in real LLM synthesis is "implement one class + flip
``MDX_SYNTHESIS_PROVIDER``".

Invariants the mock guarantees (relied on by the unit tests and by the
clinical-safety posture):

* **Deterministic** — same input → same output, no randomness, no clock.
* **No invention** — it only normalises whitespace, capitalises sentence
  starts and trims; it never adds, drops, or reorders clinical content.
* **Marker-preserving** — low-confidence ``[[ ... ]]`` spans (emitted by
  the ASR/NLP pipeline) pass through verbatim, byte-for-byte.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

# Low-confidence markers emitted upstream, e.g. ``[[unclear word]]``.
# DOTALL so a marker may span newlines; non-greedy so adjacent markers
# don't get merged into one.
_MARKER_RE = re.compile(r"\[\[.*?\]\]", re.DOTALL)
_WS_RE = re.compile(r"\s+")


@runtime_checkable
class Synthesizer(Protocol):
    """Turns one section's raw dictation into clean prose."""

    def synthesize_section(
        self,
        *,
        section_key: str,
        raw_text: str,
        synthesis_prompt: str,
        asr_prompt: str,
        language: str,
    ) -> str:
        """Return the synthesised text for a single section."""
        ...


def _capitalise_segment(segment: str, cap_next: bool) -> tuple[str, bool]:
    """Capitalise sentence starts within a plain-text segment.

    Carries a ``cap_next`` flag in/out so capitalisation state survives
    across the marker boundaries we splice around (markers are opaque and
    never alter the flag). The first alphabetic character after a sentence
    terminator (``.``/``!``/``?``) — and at the very start — is upper-cased.
    """
    out: list[str] = []
    for ch in segment:
        if ch.isalpha():
            if cap_next:
                out.append(ch.upper())
                cap_next = False
            else:
                out.append(ch)
        else:
            out.append(ch)
            if ch in ".!?":
                cap_next = True
    return "".join(out), cap_next


def _clean(text: str) -> str:
    """Deterministic light clean that preserves ``[[ ... ]]`` markers verbatim."""
    # Split into alternating (plain-text, marker) parts so markers are
    # never touched by whitespace-collapsing or capitalisation.
    parts: list[tuple[bool, str]] = []  # (is_marker, value)
    last = 0
    for m in _MARKER_RE.finditer(text):
        parts.append((False, text[last : m.start()]))
        parts.append((True, m.group(0)))
        last = m.end()
    parts.append((False, text[last:]))

    # Collapse internal whitespace in plain-text parts; keep markers as-is.
    collapsed = "".join(
        value if is_marker else _WS_RE.sub(" ", value) for is_marker, value in parts
    ).strip()

    # Re-split the (now whitespace-normalised) string and capitalise sentence
    # starts only inside plain-text spans, threading the cap_next state.
    out: list[str] = []
    cap_next = True
    last = 0
    for m in _MARKER_RE.finditer(collapsed):
        seg, cap_next = _capitalise_segment(collapsed[last : m.start()], cap_next)
        out.append(seg)
        out.append(m.group(0))  # marker verbatim
        last = m.end()
    tail, _ = _capitalise_segment(collapsed[last:], cap_next)
    out.append(tail)
    return "".join(out)


class MockSynthesizer:
    """Deterministic, offline default. See module docstring for invariants."""

    def synthesize_section(
        self,
        *,
        section_key: str,
        raw_text: str,
        synthesis_prompt: str,
        asr_prompt: str,
        language: str,
    ) -> str:
        return _clean(raw_text)


class AnthropicSynthesizer:
    """Production LLM path — **stub** pending compliance sign-off.

    Production synthesis uses Claude Opus 4.x (model id ``claude-opus-4-8``)
    to rewrite raw dictation into clean clinical prose. That sends PHH/PHI
    (the transcript) outside the box, so it is gated behind an explicit
    compliance sign-off and is intentionally not implemented here. We do
    NOT import ``anthropic``; implementing this class + flipping
    ``MDX_SYNTHESIS_PROVIDER=anthropic`` is the entire production switch.
    """

    def __init__(self, *, model: str) -> None:
        self.model = model

    def synthesize_section(
        self,
        *,
        section_key: str,
        raw_text: str,
        synthesis_prompt: str,
        asr_prompt: str,
        language: str,
    ) -> str:
        raise NotImplementedError("real LLM synthesis pending compliance sign-off")


def build_synthesizer(provider: str, model: str) -> Synthesizer:
    """Select the synthesizer implementation from config."""
    if provider == "anthropic":
        return AnthropicSynthesizer(model=model)
    return MockSynthesizer()
