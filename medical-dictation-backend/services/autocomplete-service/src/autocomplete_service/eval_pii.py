"""The eval corpus's PII sweep — at the write boundary and again at publish.

Until 0091 the corpus could only contain text vendored in the repo, so the
CI sweep (``scripts/eval/check_corpus_pii.py``) was explicitly a backstop:
"real anonymisation is done by the linguist at authorship". Console
authoring changes that. A clinician can now type a line, and "I'll be
careful" is not a control, so the same sweep runs server-side — twice:

  · on every authored/ad-hoc write, so PII never becomes a row;
  · over the whole set at publish, so a snapshot is provably clean as a
    unit (a line written before a pattern was added would otherwise sail
    through on the strength of having been written earlier).

It is a SUPERSET of the CI gate on purpose. If this sweep passed something
``check_corpus_pii.py`` later rejects, the operator would discover it at
merge time with the audio already recorded and the snapshot already scored.
So it runs the telemetry scrubber's patterns (email, IPN, med id, passport,
phone, DOB-like) AND the CI gate's context-term rule.

What it is not: a name detector. No regex separates "Пацієнт скаржиться" from
"Пацієнт Шевченко скаржиться", and pretending otherwise would make the
attestation feel redundant when it is in fact the only control over names.
The ad-hoc path therefore demands an explicit no-patient-data attestation,
recorded in the audit trail, in addition to this sweep.
"""

from __future__ import annotations

import re
from typing import Final

from .scrubber import contains_pii

# scripts/eval/check_corpus_pii.py: a 7–13 digit run introduced by an
# identifier word. The scrubber's phone pattern catches most of these; the
# rule stays so the two sweeps cannot disagree about a document.
_CONTEXT_TERMS: Final = re.compile(
    r"(іпн|id|passport|номер|серія|посвідчення)[\s:№#]*?(\d{7,13})",
    re.IGNORECASE | re.UNICODE,
)


def sweep(text: str) -> list[str]:
    """Pattern classes matched, deduplicated, stable order. Empty = clean.

    Never returns the matched text: a PII finding travels to a 422 body, an
    audit payload and a metric label, and the match itself must not be in
    any of them.
    """
    found = list(contains_pii(text))
    if _CONTEXT_TERMS.search(text) and "id_context" not in found:
        found.append("id_context")
    return found
