"""Inline-completion prompt builder.

The frame is deliberately narrow: continue the clinician's CURRENT
sentence in clinical register — never introduce clinical facts. The
"no new facts" instruction is belt; the output safety filter
(``safety_filter.py``) is suspenders — the model is never trusted to
follow the instruction (sprint-12 anti-hallucination framing carried
into Layer C: the model proposes, the clinician disposes).
"""

from __future__ import annotations

from typing import Literal

Language = Literal["uk", "en"]

_FRAME_UK = (
    "Ти — асистент лікаря, який продовжує речення у клінічному документі. "
    "Продовжуй поточне речення природною українською медичною мовою, у "
    "клінічному стилі. Не вигадуй жодних нових клінічних фактів: жодних "
    "числових показників, дозувань, діагнозів чи кодів, яких немає у тексті. "
    "Заверши лише граматичну структуру речення. Відповідай тільки "
    "продовженням речення, без пояснень і без повторення вже написаного."
)

_FRAME_EN = (
    "You are a clinician's assistant continuing a sentence in a clinical "
    "document. Continue the current sentence in natural clinical English. "
    "Do not invent any new clinical facts: no numeric values, dosages, "
    "diagnoses or codes that are not already in the text. Complete only the "
    "grammatical structure of the sentence. Reply with the continuation "
    "only — no explanations, no repetition of what is already written."
)

_SECTION_LABEL = {"uk": "Розділ документа", "en": "Document section"}
_TEXT_LABEL = {"uk": "Текст до курсора", "en": "Text before cursor"}


def build_prompt(
    *, section_key: str, text_before_cursor: str, language: Language
) -> str:
    frame = _FRAME_UK if language == "uk" else _FRAME_EN
    return (
        f"{frame}\n\n"
        f"{_SECTION_LABEL[language]}: {section_key}\n\n"
        f"{_TEXT_LABEL[language]}:\n{text_before_cursor}"
    )
