"""Prompt-building for Whisper's ``initial_prompt`` across windows.

First window: use the clinician-specialty prompt (sprint 03
``medical_prompts``).

Subsequent windows: append the last N tokens of the FINALIZED transcript
so Whisper has decoded-context biasing without re-feeding the audio.
Voice-command tokens (sprint 05) are stripped — they're rendered text,
not literal dictation.

The combined prompt is capped at 224 tokens (Whisper's hard limit) via
``truncate_to_tokens`` — a coarse whitespace tokenisation that's close
enough to BPE for biasing purposes.
"""

from __future__ import annotations

import re

from asr_models import WordTiming

_VOICE_COMMAND_PATTERNS = (
    "[new paragraph]",
    "[period]",
    "[comma]",
)


def build_prompt(
    *,
    base_prompt: str | None,
    finalized_words: list[WordTiming],
    max_total_tokens: int = 224,
) -> str | None:
    """Compose base + last-finalized for the next window."""
    if not base_prompt and not finalized_words:
        return None
    base = base_prompt or ""
    base_tokens = _whitespace_tokens(base)
    remaining = max_total_tokens - len(base_tokens)
    if remaining <= 0:
        return base.strip() or None

    tail_text = " ".join(w.text for w in finalized_words if w.text)
    tail_text = _strip_voice_commands(tail_text)
    tail_tokens = _whitespace_tokens(tail_text)[-remaining:]
    parts = [t for t in (base.strip(), " ".join(tail_tokens).strip()) if t]
    return " ".join(parts).strip() or None


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    tokens = _whitespace_tokens(text)
    if len(tokens) <= max_tokens:
        return text
    return " ".join(tokens[-max_tokens:])


def _whitespace_tokens(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", text or "") if t]


def _strip_voice_commands(text: str) -> str:
    out = text
    for pat in _VOICE_COMMAND_PATTERNS:
        out = out.replace(pat, "")
    # Also strip Whisper special tokens defensively.
    return re.sub(r"<\|[^|]+\|>", "", out).strip()
