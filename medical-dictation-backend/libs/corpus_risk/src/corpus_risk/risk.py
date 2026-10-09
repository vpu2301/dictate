"""Risk-flag detection — lexicon + regex, biased to over-flag (ADR-0043 §6).

A false tier-3 costs the clinician 15 seconds; a missed flag is how a wrong
dose reaches a clinician's cursor. When in doubt, flag.

Flags: 'dose', 'drug', 'laterality', 'negation', 'icd', 'abbrev'.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Final

from corpus_risk.normalize import tokenize

#: The closed vocabulary, in the order `flags()` emits it. Mirrored by the
#: migration-0082 CHECK and by src/api/corpus.js RISK_FLAGS in the console.
RISK_FLAGS: Final = ("dose", "drug", "laterality", "negation", "icd", "abbrev")

#: The reviewed wordlists travel WITH this package rather than sitting in
#: infra/seeds, because both ingest paths need them at runtime and only one of
#: them (the CLI) runs from a repo checkout. Editing them is still a clinical
#: review under ADR-0043 — see the header of each file.
_DATA_DIR: Final = Path(__file__).parent / "data"

# Any digit is dose-adjacent in clinical text (BP, doses, frequencies) —
# deliberately broad. Unit words catch spelled-out quantities.
_DIGITS: Final = re.compile(r"\d")
_DOSE_UNITS: Final = re.compile(
    r"\b(мг|мкг|мл|гр?|од|таб(л(етк[аиі])?)?|капс(ул[аиі])?|крапл[іь]|амп(ул[аиі])?"
    r"|mg|mcg|µg|ml|iu|units?|tab(let)?s?|caps(ule)?s?|drops?)\b",
    re.IGNORECASE,
)

# Word-start stems, uk + en.
_LATERALITY: Final = re.compile(
    r"\b(лів|прав|зліва|справа|білатерал|двобічн|left|right|bilateral)", re.IGNORECASE
)

_NEGATION: Final = re.compile(
    r"\b(не|ні|ані|без|немає|відсутн\w*|заперечує|виключено"
    r"|no|not|none|without|denies|negative|absent)\b",
    re.IGNORECASE,
)

# ICD-10-ish code: Latin (or lookalike Cyrillic) letter + 2 digits + optional .N(N).
_ICD: Final = re.compile(r"\b[A-ZА-ЯЇІЄҐ]\d{2}(?:\.\d{1,2})?\b")

# Unfamiliar abbreviation: 2–5 letter all-caps token not in the reviewed
# allowlist. The allowlist starts small on purpose — over-flag bias.
_ABBREV_TOKEN: Final = re.compile(r"^[A-ZА-ЯЇІЄҐ]{2,5}$")


def load_wordlist(path: Path) -> frozenset[str]:
    """One entry per line; '#' comments and blanks ignored; lowercased."""
    entries: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip().lower()
        if entry:
            entries.add(entry)
    return frozenset(entries)


class RiskFlagger:
    def __init__(
        self,
        *,
        drug_lexicon: frozenset[str] = frozenset(),
        abbrev_allowlist: frozenset[str] = frozenset(),
    ) -> None:
        self._drugs = drug_lexicon
        self._abbrev_ok = {a.upper() for a in abbrev_allowlist}

    def flags(self, phrase: str) -> list[str]:
        found: list[str] = []
        if _DIGITS.search(phrase) or _DOSE_UNITS.search(phrase):
            found.append("dose")
        if self._drugs and any(tok.lower() in self._drugs for tok in tokenize(phrase)):
            found.append("drug")
        if _LATERALITY.search(phrase):
            found.append("laterality")
        if _NEGATION.search(phrase):
            found.append("negation")
        if _ICD.search(phrase):
            found.append("icd")
        if any(
            _ABBREV_TOKEN.match(raw) and raw not in self._abbrev_ok
            # raw tokens, not normalised: case is the signal here
            for raw in re.findall(r"[^\W\d_]+", phrase)
        ):
            found.append("abbrev")
        return found


@lru_cache(maxsize=1)
def default_flagger() -> RiskFlagger:
    """The flagger every ingest path must use.

    Cached because the wordlists are read-only files and the HTTP path calls
    this once per submitted phrase. A caller that wants a different lexicon
    (a test, an import that just grew the drug list) constructs RiskFlagger
    directly — but nothing in production should, or the tier a phrase gets
    would depend on which door it came through.
    """
    drugs = _DATA_DIR / "drug_stems.txt"
    abbrevs = _DATA_DIR / "abbrev_allowlist.txt"
    return RiskFlagger(
        drug_lexicon=load_wordlist(drugs) if drugs.exists() else frozenset(),
        abbrev_allowlist=load_wordlist(abbrevs) if abbrevs.exists() else frozenset(),
    )
