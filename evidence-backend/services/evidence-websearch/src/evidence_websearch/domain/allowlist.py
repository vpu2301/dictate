"""Domain allowlist resolution (FR-6).

Rows live in `web_domains`; the shipped, clinically reviewed set is seeded
under the reserved GLOBAL tenant (migration 0070) and every tenant reads it.
Resolution is **tenant-first**: a tenant row for a domain shadows the global
row of the same name, which is how a tenant disables a shipped default (or
re-tiers it) without touching shared data.

Matching is exact-host or subdomain-of (`host == domain` or
`host.endswith("." + domain)`). No wildcards, no substring matching —
`evil-who.int.example.com` must not pass because `who.int` is on the list.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import asyncpg

from evidence_models import WebTrustTier
from evidence_websearch.adapters import pg

GLOBAL_TENANT = UUID("00000000-0000-0000-0000-000000000000")


@dataclass(frozen=True, slots=True)
class DomainRule:
    domain: str
    trust_tier: WebTrustTier
    status: str
    is_default: bool
    # Paywalled/licensed sources: cite title + URL, never body text (§7).
    metadata_only: bool


@dataclass(frozen=True, slots=True)
class Allowlist:
    """An immutable snapshot of one tenant's effective allowlist."""

    rules: dict[str, DomainRule]

    @classmethod
    def merge(cls, *, shipped: list[DomainRule], tenant: list[DomainRule]) -> Allowlist:
        """Overlay the tenant's rows on the shipped GLOBAL ones.

        The two sides are separate parameters rather than one list plus a
        set of "tenant-owned" names: with a single list the result depended
        on row order, so the same data could resolve differently depending on
        how the query happened to sort. Ownership decides the winner, never
        position.
        """
        merged = {rule.domain: rule for rule in shipped}
        merged.update({rule.domain: rule for rule in tenant})
        return cls(rules=merged)

    def match(self, host: str) -> DomainRule | None:
        """Return the enabled rule covering `host`, or None.

        Longest match wins so a tenant can enable `ncbi.nlm.nih.gov` broadly
        while pinning `pubmed.ncbi.nlm.nih.gov` to metadata-only.
        """
        host = host.strip().rstrip(".").casefold()
        if not host:
            return None
        best: DomainRule | None = None
        for domain, rule in self.rules.items():
            covers = host == domain or host.endswith("." + domain)
            if covers and (best is None or len(domain) > len(best.domain)):
                best = rule
        if best is None or best.status != "enabled":
            return None
        return best

    def is_allowed(self, host: str) -> bool:
        return self.match(host) is not None


async def load_for_tenant(conn: asyncpg.Connection, *, tenant_id: UUID) -> Allowlist:
    """Effective allowlist for one tenant.

    RLS already limits the read to {caller tenant, GLOBAL}; the tenant-wins
    merge happens here rather than in SQL because expressing "shadow by name"
    as a window function would be harder to read than five lines of Python.
    """
    rows = await pg.list_domains(conn)

    def to_rule(row: pg.DomainRow) -> DomainRule:
        return DomainRule(
            domain=row.domain,
            trust_tier=WebTrustTier(row.trust_tier),
            status=row.status,
            is_default=row.is_default,
            metadata_only=row.metadata_only,
        )

    return Allowlist.merge(
        shipped=[to_rule(r) for r in rows if r.tenant_id == GLOBAL_TENANT],
        tenant=[to_rule(r) for r in rows if r.tenant_id != GLOBAL_TENANT],
    )
