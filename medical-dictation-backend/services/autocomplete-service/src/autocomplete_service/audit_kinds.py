"""Audit kinds emitted by autocomplete-service."""

from __future__ import annotations

from typing import Final

PHRASE_CREATED: Final = "autocomplete.phrase.created"
PHRASE_UPDATED: Final = "autocomplete.phrase.updated"
PHRASE_DELETED: Final = "autocomplete.phrase.deleted"
SNIPPET_CREATED: Final = "autocomplete.snippet.created"
SNIPPET_UPDATED: Final = "autocomplete.snippet.updated"
SNIPPET_DELETED: Final = "autocomplete.snippet.deleted"
PHRASE_WRITE_REJECTED_PII: Final = "autocomplete.phrase.write_rejected_pii"
ROLLUP_COMPLETED: Final = "autocomplete.rollup.completed"
