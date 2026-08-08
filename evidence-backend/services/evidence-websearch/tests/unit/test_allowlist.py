"""Allowlist matching — the difference between "on the list" and "looks like
it is on the list"."""

from __future__ import annotations

from evidence_websearch.domain.allowlist import Allowlist, DomainRule

from evidence_models import WebTrustTier


def rule(domain: str, *, status: str = "enabled", metadata_only: bool = False) -> DomainRule:
    return DomainRule(
        domain=domain,
        trust_tier=WebTrustTier.government,
        status=status,
        is_default=True,
        metadata_only=metadata_only,
    )


def test_exact_host_matches() -> None:
    allowlist = Allowlist(rules={"who.int": rule("who.int")})
    assert allowlist.is_allowed("who.int")


def test_subdomain_matches_but_suffix_lookalike_does_not() -> None:
    allowlist = Allowlist(rules={"who.int": rule("who.int")})

    assert allowlist.is_allowed("iris.who.int")
    # The classic homograph-adjacent trick: the allowlisted string appears in
    # the host, but not as a domain suffix.
    assert not allowlist.is_allowed("evil-who.int.attacker.com")
    assert not allowlist.is_allowed("whoxint.com")
    assert not allowlist.is_allowed("notwho.int")


def test_trailing_dot_and_case_are_normalized() -> None:
    allowlist = Allowlist(rules={"who.int": rule("who.int")})
    assert allowlist.is_allowed("WHO.INT.")
    assert allowlist.is_allowed("  Iris.WHO.int  ")


def test_disabled_rule_does_not_match() -> None:
    allowlist = Allowlist(rules={"who.int": rule("who.int", status="disabled")})
    assert allowlist.match("who.int") is None
    assert allowlist.match("iris.who.int") is None


def test_longest_match_wins_so_a_narrow_rule_can_override_a_broad_one() -> None:
    """A tenant enables all of ncbi.nlm.nih.gov but pins the PubMed abstract
    pages to metadata-only. The narrower rule must win."""
    allowlist = Allowlist(
        rules={
            "ncbi.nlm.nih.gov": rule("ncbi.nlm.nih.gov"),
            "pubmed.ncbi.nlm.nih.gov": rule("pubmed.ncbi.nlm.nih.gov", metadata_only=True),
        }
    )

    broad = allowlist.match("pmc.ncbi.nlm.nih.gov")
    narrow = allowlist.match("pubmed.ncbi.nlm.nih.gov")

    assert broad is not None and broad.metadata_only is False
    assert narrow is not None and narrow.metadata_only is True


def test_tenant_row_shadows_the_shipped_global_row() -> None:
    """How a tenant turns off a shipped default without touching shared data."""
    merged = Allowlist.merge(shipped=[rule("bmj.com")], tenant=[rule("bmj.com", status="disabled")])

    assert merged.match("bmj.com") is None


def test_merge_result_does_not_depend_on_row_order() -> None:
    """Ownership decides the winner, never position in the result set."""
    shipped = [rule("nejm.org"), rule("who.int")]
    tenant = [rule("nejm.org", status="disabled")]

    forwards = Allowlist.merge(shipped=shipped, tenant=tenant)
    backwards = Allowlist.merge(shipped=list(reversed(shipped)), tenant=tenant)

    assert forwards.rules == backwards.rules
    assert forwards.match("nejm.org") is None
    assert forwards.is_allowed("who.int")


def test_tenant_can_add_a_domain_the_shipped_list_never_had() -> None:
    merged = Allowlist.merge(shipped=[rule("who.int")], tenant=[rule("moz.gov.ua")])

    assert merged.is_allowed("who.int")
    assert merged.is_allowed("moz.gov.ua")


def test_empty_host_is_never_allowed() -> None:
    allowlist = Allowlist(rules={"who.int": rule("who.int")})
    assert not allowlist.is_allowed("")
    assert not allowlist.is_allowed("   ")
