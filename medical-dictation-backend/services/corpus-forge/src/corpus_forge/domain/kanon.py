"""K-anonymity gate — defense-in-depth re-check of the SQL HAVING clause.

A phrase only one doctor uses is that doctor's style, and possibly
identifying (ADR-0043 §7). The SQL gate is authoritative; this re-check
exists so a future refactor of the mining query can't silently drop it.
"""

from __future__ import annotations

from dataclasses import dataclass

K_MIN_AUTHORS = 5
K_MIN_TENANTS = 2


@dataclass(frozen=True, slots=True)
class MinedStats:
    frequency: int
    distinct_authors: int
    distinct_tenants: int


def passes_k_anonymity(
    stats: MinedStats,
    *,
    min_authors: int = K_MIN_AUTHORS,
    min_tenants: int = K_MIN_TENANTS,
) -> bool:
    return stats.distinct_authors >= min_authors and stats.distinct_tenants >= min_tenants
