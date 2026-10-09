"""corpus_risk — the risk flagger and the tier router, in one place.

WHY THIS IS A LIB AND NOT A MODULE INSIDE corpus-forge
------------------------------------------------------

A corpus candidate's tier is a safety decision: "any risk flag ⇒ a human must
read this phrase before a clinician's cursor can ever autocomplete it"
(ADR-0043 §6, migration 0082). Until now that decision was made in exactly one
place — the corpus-forge CLI — while a SECOND ingest path existed:
`POST /corpus/candidates`, the console fill worksheet, added in migration
0088. That path never ran the flagger. It wrote `tier = 2, risk_flags = '{}'`
for every phrase, including doses, drugs, laterality and negations, and since
the human review queue serves `tier = 3` those phrases reached no reviewer at
all.

Two ingest paths, two answers to the same safety question, is the bug. So the
answer moved here and both paths import it:

    corpus-forge          → mine / import / generate  (CLI)
    autocomplete-service  → POST /corpus/candidates   (console worksheet)

Nothing in this package touches the database or the network; it is pure text
in, flags and a tier out, which is what makes it cheap enough for both.
"""

from corpus_risk.normalize import (
    collapse_whitespace,
    dedupe_key,
    normalize_apostrophe,
    tokenize,
)
from corpus_risk.risk import RISK_FLAGS, RiskFlagger, default_flagger, load_wordlist
from corpus_risk.tiers import (
    SOURCE_KINDS,
    TIER_AUTO,
    TIER_HUMAN_MANDATORY,
    TIER_MACHINE_REVIEWED,
    route_tier,
)

__all__ = [
    "RISK_FLAGS",
    "SOURCE_KINDS",
    "TIER_AUTO",
    "TIER_HUMAN_MANDATORY",
    "TIER_MACHINE_REVIEWED",
    "RiskFlagger",
    "collapse_whitespace",
    "dedupe_key",
    "default_flagger",
    "load_wordlist",
    "normalize_apostrophe",
    "route_tier",
    "tokenize",
]
