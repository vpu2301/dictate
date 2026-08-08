"""Cross-module constants."""

from __future__ import annotations

from uuid import UUID

# Reserved owner of the shared global corpus (migration 0068): readable by
# every tenant, writable only when the operator explicitly scopes to it.
GLOBAL_TENANT = UUID("00000000-0000-0000-0000-000000000000")

# Snapshot admission (rule / spec §7: unlicensed content never ships).
# 'restricted' is also the parking value for license-unknown ingests — such
# documents are stored and indexed for review but excluded from snapshots.
ALLOWED_SNAPSHOT_LICENSES = frozenset(
    {"public_domain", "open_license", "licensed_redistributable", "licensed_internal"}
)
