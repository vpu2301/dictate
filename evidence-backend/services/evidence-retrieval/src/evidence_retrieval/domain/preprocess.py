"""Query preprocessing: normalization + uk/en clinical lexicon v1 (spec D7).

The lexicon expands common clinical abbreviations into full terms for the
LEXICAL path only (dense embeddings handle surface variation natively).
lexicon_version participates in the cache key and the determinism contract.

v1 is a curated seed (clinical review pending — sign-off register row);
tenant-specific abbreviation tables join in a later sprint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

LEXICON_VERSION = "1.0"

# abbreviation (lowercased) -> expansion
_LEXICON: dict[str, str] = {
    # Ukrainian
    "хгн": "хронічний гломерулонефрит",
    "ххн": "хронічна хвороба нирок",
    "фп": "фібриляція передсердь",
    "гх": "гіпертонічна хвороба",
    "аг": "артеріальна гіпертензія",
    "цд": "цукровий діабет",
    "ішс": "ішемічна хвороба серця",
    "іхс": "ішемічна хвороба серця",
    "гім": "гострий інфаркт міокарда",
    "тела": "тромбоемболія легеневої артерії",
    "шкф": "швидкість клубочкової фільтрації",
    "ат": "артеріальний тиск",
    "хсн": "хронічна серцева недостатність",
    "гдн": "гостра дихальна недостатність",
    "хозл": "хронічне обструктивне захворювання легень",
    "нпзп": "нестероїдні протизапальні препарати",
    "інгал": "інгаляційний",
    "в/в": "внутрішньовенно",
    "в/м": "внутрішньом'язово",
    # English
    "ckd": "chronic kidney disease",
    "af": "atrial fibrillation",
    "afib": "atrial fibrillation",
    "htn": "hypertension",
    "dm": "diabetes mellitus",
    "t2dm": "type 2 diabetes mellitus",
    "cad": "coronary artery disease",
    "mi": "myocardial infarction",
    "pe": "pulmonary embolism",
    "dvt": "deep vein thrombosis",
    "egfr": "estimated glomerular filtration rate",
    "bp": "blood pressure",
    "chf": "chronic heart failure",
    "hf": "heart failure",
    "copd": "chronic obstructive pulmonary disease",
    "nsaid": "nonsteroidal anti-inflammatory drug",
    "nsaids": "nonsteroidal anti-inflammatory drugs",
    "doac": "direct oral anticoagulant",
    "acei": "angiotensin-converting enzyme inhibitor",
    "arb": "angiotensin receptor blocker",
    "uti": "urinary tract infection",
}

_TOKEN = re.compile(r"[a-zа-щьюяіїєґ0-9/']+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PreparedQuery:
    raw: str
    normalized: str
    # Expansion terms appended for the lexical engine only.
    expanded_terms: list[str] = field(default_factory=list)
    lang_guess: str | None = None
    lexicon_version: str = LEXICON_VERSION

    @property
    def lexical_query(self) -> str:
        if not self.expanded_terms:
            return self.normalized
        return self.normalized + " " + " ".join(self.expanded_terms)


def _guess_lang(text: str) -> str | None:
    cyr = len(re.findall(r"[а-яіїєґ]", text, re.IGNORECASE))
    lat = len(re.findall(r"[a-z]", text, re.IGNORECASE))
    if cyr + lat < 3:
        return None
    return "uk" if cyr >= lat else "en"


def prepare(query: str) -> PreparedQuery:
    normalized = " ".join(query.strip().split())
    tokens = [t.lower() for t in _TOKEN.findall(normalized)]
    expansions: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        expansion = _LEXICON.get(token)
        if expansion and expansion not in seen:
            expansions.append(expansion)
            seen.add(expansion)
    return PreparedQuery(
        raw=query,
        normalized=normalized,
        expanded_terms=expansions,
        lang_guess=_guess_lang(normalized),
    )
