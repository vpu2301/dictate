"""Prompt-injection screen (rule LM4), pattern set v1.

Retrieved content is data, not instructions. A corpus document that carries
instruction-shaped payloads is quarantined before it can reach an index (a
knowledge_admin reviews and decides); a fetched web page that trips the
screen is dropped outright — nobody curates the open web, so there is no
review queue to send it to (EVA-S04). Patterns aim at instruction takeover
phrasing (en+uk), chat-template markers, and tool-call syntax; benign
clinical uses of words like "instructions" must NOT trigger (tested against
a lookalike fixture set).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ignore_previous_en",
        re.compile(
            r"\b(ignore|disregard|forget)\b[^.\n]{0,40}\b(previous|prior|above|all)\b[^.\n]{0,40}\b(instructions?|rules|prompts?|context)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ignore_previous_uk",
        re.compile(
            r"\b(ігноруй|забудь|знехтуй)\b[^.\n]{0,60}\b(попередні|усі|всі|свої)\b[^.\n]{0,60}\b(інструкції|правила|налаштування|команди)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt_reveal",
        re.compile(
            r"\b(reveal|show|print|repeat)\b[^.\n]{0,40}\b(system\s+prompt|hidden\s+instructions)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "system_role_marker",
        re.compile(
            r"(<\|im_start\|>\s*system|<<SYS>>|\[INST\]|<\|system\|>|^SYSTEM\s*:|\bsystem\s+prompt\s*:)",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "mode_switch",
        re.compile(
            r"\b(you\s+are\s+now|act\s+as|developer\s+mode|jailbreak|ти\s+більше\s+не|уяви,?\s+що\s+ти)\b[^.\n]{0,60}\b(mode|admin|administrator|system|асистент|адміністратор|режим[іеу]?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "tool_call_syntax",
        re.compile(
            r"\{\s*\"(tool|function|tool_calls|name)\"\s*:\s*\"?(execute|call|run|delete|drop)",
            re.IGNORECASE,
        ),
    ),
    (
        "function_invocation",
        re.compile(
            r"\b(call|invoke|execute)\b[^.\n]{0,30}\b(the\s+)?(function|tool)\b[^.\n]{0,40}\b(delete|drop|disable|exfiltrate|send)",
            re.IGNORECASE,
        ),
    ),
    (
        "safety_disable",
        re.compile(
            r"\b(disable|bypass|turn\s+off|вимкни|обійди)\b[^.\n]{0,40}\b(safety|guardrails?|filters?|checks?|захист|перевірки|обмеження)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "data_exfiltration_uk",
        re.compile(
            r"\b(надішли|передай|відправ)\b[^.\n]{0,60}\b(дані|інформацію)\b[^.\n]{0,40}\b(пацієнт)",
            re.IGNORECASE,
        ),
    ),
    (
        "forced_verdict_uk",
        re.compile(r"\bвідповідай\s+(лише|тільки)\b", re.IGNORECASE),
    ),
]

_EXCERPT_RADIUS = 60


@dataclass(frozen=True, slots=True)
class InjectionHit:
    pattern: str
    excerpt: str


def screen_text(text: str) -> list[InjectionHit]:
    hits: list[InjectionHit] = []
    for name, pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - _EXCERPT_RADIUS)
            end = min(len(text), match.end() + _EXCERPT_RADIUS)
            hits.append(InjectionHit(pattern=name, excerpt=text[start:end].strip()))
    return hits
