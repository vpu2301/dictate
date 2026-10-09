"""K-anonymity gate — the spec case: 4 authors → rejected, 5 → accepted."""

from corpus_forge.domain.kanon import MinedStats, passes_k_anonymity


def test_four_authors_rejected() -> None:
    assert not passes_k_anonymity(MinedStats(frequency=100, distinct_authors=4, distinct_tenants=3))


def test_five_authors_two_tenants_accepted() -> None:
    assert passes_k_anonymity(MinedStats(frequency=5, distinct_authors=5, distinct_tenants=2))


def test_one_tenant_rejected_regardless_of_authors() -> None:
    assert not passes_k_anonymity(MinedStats(frequency=999, distinct_authors=50, distinct_tenants=1))


def test_thresholds_are_parameters() -> None:
    stats = MinedStats(frequency=1, distinct_authors=3, distinct_tenants=1)
    assert passes_k_anonymity(stats, min_authors=3, min_tenants=1)
    assert not passes_k_anonymity(stats, min_authors=4, min_tenants=1)
