"""Text normalization shared by every pipeline stage.

`dedupe_key` is THE identity of a candidate: lowercased, whitespace-collapsed,
apostrophe-normalised. Two spellings that normalise equal are one candidate.

Moved here from corpus_forge.domain.normalize when the HTTP ingest path needed
the same tokenizer the flagger runs on (see this package's __init__).
"""

from __future__ import annotations

import re
from typing import Final

# Ukrainian text uses the modifier apostrophe ’ (U+2019); imports and LLM
# output frequently carry ASCII ' or U+02BC. Normalise to ’ — the same
# convention the corpus validator enforces on seed files.
_APOSTROPHES: Final = re.compile(r"['ʼ‘‛`]")
_WHITESPACE: Final = re.compile(r"\s+")

# Tokens: letters/digits plus in-word apostrophe and hyphen
# (сім’я, ліво-правий). Everything else separates.
_TOKEN: Final = re.compile(r"[^\W_]+(?:[’'-][^\W_]+)*", re.UNICODE)


def normalize_apostrophe(text: str) -> str:
    return _APOSTROPHES.sub("’", text)


def collapse_whitespace(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def dedupe_key(phrase: str) -> str:
    return collapse_whitespace(normalize_apostrophe(phrase).lower())


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(normalize_apostrophe(text))
