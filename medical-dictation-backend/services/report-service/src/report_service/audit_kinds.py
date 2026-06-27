"""Audit kinds emitted by report-service. See docs/audit/event-kinds.md."""

from __future__ import annotations

from typing import Final

TEMPLATE_CREATED: Final = "template.created"  # plain POST /templates (M1·A4)
TEMPLATE_CLONED: Final = "template.cloned"
TEMPLATE_UPDATED: Final = "template.updated"  # cosmetic edit
TEMPLATE_VERSIONED: Final = "template.versioned"  # structural edit → new row
TEMPLATE_DEPRECATED: Final = "template.deprecated"  # soft-delete
TEMPLATE_VIEWED_FULL: Final = "template.viewed_full"  # GET /templates/{id}

# Sprint-08: reports slice.
REPORT_CREATED: Final = "report.created"
REPORT_DRAFT_UPDATED: Final = "report.draft.updated"  # aggregated per session
REPORT_FINALIZED: Final = "report.finalized"
REPORT_REVERTED: Final = "report.reverted"
REPORT_CANCELLED: Final = "report.cancelled"
REPORT_AMENDED: Final = "report.amended"  # post-sign (sprint-09)
REPORT_AMENDMENT_DRAFTED: Final = "report.amendment_drafted"  # pre-sign
REPORT_VIEWED_FULL: Final = "report.viewed_full"  # carries purpose
REPORT_SEARCHED: Final = "report.searched"
REPORT_CHAIN_INTEGRITY_FAILURE: Final = "report.chain_integrity_failure"
REPORT_PDF_RENDERED: Final = "report.pdf_rendered"  # GET /reports/{id}/pdf (M1·A3)
