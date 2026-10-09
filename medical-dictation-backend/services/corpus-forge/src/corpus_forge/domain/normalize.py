"""Text normalization — re-exported from the shared corpus_risk lib.

The implementation moved to `corpus_risk.normalize` when the HTTP ingest path
(POST /corpus/candidates, autocomplete-service) had to compute the same
`dedupe_key` and run the same tokenizer as the CLI. Two copies of a dedupe key
means two candidate identities for one phrase, so there is one copy and this
module points at it.
"""

from __future__ import annotations

from corpus_risk.normalize import (
    collapse_whitespace,
    dedupe_key,
    normalize_apostrophe,
    tokenize,
)

__all__ = ["collapse_whitespace", "dedupe_key", "normalize_apostrophe", "tokenize"]
