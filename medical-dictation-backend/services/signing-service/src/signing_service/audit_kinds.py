"""Audit kinds emitted by signing-service."""

from __future__ import annotations

from typing import Final

# Tenant-scoped audit chain.
SIGNING_SESSION_INITIATED: Final = "signing.session.initiated"
SIGNING_SESSION_EXPIRED: Final = "signing.session.expired"
SIGNING_SESSION_REJECTED: Final = "signing.session.rejected"
SIGNING_SESSION_FAILED: Final = "signing.session.failed"
SIGNING_SESSION_CANCELLED: Final = "signing.session.cancelled"  # user abort (M1·B2)
SIGNING_SESSION_LOCAL_UPLOAD: Final = "signing.session.local_upload"  # local-KEP upload (M1·B4)
SIGNING_ENVELOPE_PERSISTED: Final = "signing.envelope.persisted"
SIGNING_PROVIDER_HEALTH_CHANGED: Final = "signing.provider.health_changed"
SIGNING_CALLBACK_SIGNATURE_INVALID: Final = "signing.session.callback_signature_invalid"
SIGNING_DEV_PASSWORD_REJECTED: Final = "signing.dev_password_rejected"  # sec (S09-rev)
SIGNING_FILE_KEY_REJECTED: Final = "signing.file_key_rejected"  # sec (S09-rev)
# HOTFIX — a principal without `report.sign` reached a signing surface.
# Emitted BEFORE provider dispatch, at `sec` severity, naming the role.
SIGNING_DENIED_ROLE: Final = "signing.denied_role"

# Global (no-tenant) verify audit stream — written to
# ``audit.public_verify_audit`` not the hash-chained log.
PUBLIC_VERIFY_LOOKUP: Final = "signing.envelope.verified_public"
PUBLIC_VERIFY_PDF_FETCH: Final = "signing.envelope.pdf_fetched_public"
