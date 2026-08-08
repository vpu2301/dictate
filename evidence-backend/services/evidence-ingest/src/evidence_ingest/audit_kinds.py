"""Audit kinds emitted by evidence-ingest (catalogued in the platform's
docs/audit/event-kinds.md, EVA-S02 section)."""

from typing import Final

DOCUMENT_INGESTED: Final = "evidence.document_ingested"
DOCUMENT_RETRACTED: Final = "evidence.document_retracted"
DOCUMENT_QUARANTINED: Final = "evidence.document_quarantined"
QUARANTINE_DECIDED: Final = "evidence.quarantine_decided"
SNAPSHOT_CREATED: Final = "evidence.snapshot_created"
