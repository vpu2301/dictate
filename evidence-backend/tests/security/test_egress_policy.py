"""Egress policy assertions (AC-S04-B-5, rule LM1).

These read the proxy's committed configuration rather than the running
container, so they hold in CI where no Docker is available. What they prove:

* the proxy denies by default and reads its allowlist from a file;
* private address space is blocked at the proxy too, not only in-process;
* **no model-vendor API is reachable** — the network is where "all models are
  self-hosted" stops being a claim and becomes a fact;
* the network allowlist and the seeded product allowlist agree, so a domain
  cannot be citable-but-unfetchable (or worse, the reverse).

The live network-policy check against a running stack lives in
`tests/integration/test_websearch_live.py` (env-gated).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SQUID_CONF = ROOT / "infra" / "compose" / "egress-proxy" / "squid.conf"
ALLOWLIST = ROOT / "infra" / "compose" / "egress-proxy" / "allowlist.txt"
MIGRATION = (
    ROOT.parent
    / "medical-dictation-backend"
    / "infra"
    / "postgres"
    / "migrations"
    / "0070_evidence_answers.sql"
)

# Every external model API we must never be able to reach. The list is
# deliberately broad: an entry here failing the test means someone added a
# vendor to the allowlist, which is an ADR-level decision (rule LM1 says the
# answer is no).
MODEL_VENDOR_HOSTS = (
    "openai.com",
    "anthropic.com",
    "googleapis.com",
    "cohere.ai",
    "cohere.com",
    "mistral.ai",
    "huggingface.co",
    "replicate.com",
    "together.ai",
    "azure.com",
    "bedrock",
    "vertexai",
)


def _allowlist_domains() -> list[str]:
    return [
        line.strip().lstrip(".")
        for line in ALLOWLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_proxy_denies_by_default() -> None:
    conf = SQUID_CONF.read_text(encoding="utf-8")
    assert "http_access deny all" in conf, "the proxy must end with a default deny"
    allow_lines = [
        line.strip() for line in conf.splitlines() if line.strip().startswith("http_access allow")
    ]
    # Exactly one allow rule, and it is gated on both the internal source ACL
    # and the destination allowlist.
    assert allow_lines == ["http_access allow internal allowed_domains"], allow_lines


def test_proxy_blocks_private_address_space() -> None:
    conf = SQUID_CONF.read_text(encoding="utf-8")
    assert "http_access deny to_private" in conf
    for cidr in ("169.254.0.0/16", "127.0.0.0/8", "10.0.0.0/8", "fc00::/7"):
        assert cidr in conf, f"private range {cidr} is not blocked at the proxy"


def test_no_model_vendor_is_reachable() -> None:
    """Rule LM1, asserted at the network boundary."""
    domains = _allowlist_domains()
    offenders = [
        domain
        for domain in domains
        if any(vendor in domain.casefold() for vendor in MODEL_VENDOR_HOSTS)
    ]
    assert not offenders, (
        f"model-vendor domains on the egress allowlist: {offenders}. "
        "All inference is self-hosted (rule LM1) — adding one is an ADR, not an edit."
    )


def test_allowlist_entries_are_hostnames_not_patterns() -> None:
    """A stray `*`, scheme or path in the file would either be ignored by
    squid (silently narrowing) or match more than intended."""
    hostname = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
    bad = [d for d in _allowlist_domains() if not hostname.match(d)]
    assert not bad, f"malformed allowlist entries: {bad}"


def test_seeded_product_domains_are_all_network_reachable() -> None:
    """The two allowlists must agree in the direction that matters.

    A domain seeded into `web_domains` but absent from the network allowlist
    would be offered to clinicians as a source and then fail every fetch.
    """
    seeded = set(
        re.findall(
            r"'00000000-0000-0000-0000-000000000000', '([a-z0-9.-]+)'", MIGRATION.read_text()
        )
    )
    assert seeded, "no seeded domains found — did the migration move?"
    network = _allowlist_domains()

    unreachable = [
        domain
        for domain in sorted(seeded)
        if not any(domain == entry or domain.endswith("." + entry) for entry in network)
    ]
    assert not unreachable, (
        f"seeded in web_domains but not reachable through the proxy: {unreachable}"
    )
