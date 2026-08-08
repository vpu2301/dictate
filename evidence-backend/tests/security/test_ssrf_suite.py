"""SSRF attack suite against the fetcher (AC-S04-B-5).

Ten attack fixtures, each a URL a hostile search result (or a hostile page's
redirect) could hand the fetcher. Every one must be refused **with a specific
reason** — a generic rejection would let a real SSRF hide among ordinary
allowlist misses in the trace.

The proxy is the enforcement boundary in production; these tests cover the
in-process layer, which is what stands if the proxy is ever misconfigured.
The proxy's own policy is asserted separately in `test_egress_policy.py`.

Run: `make security-web`.
"""

from __future__ import annotations

import httpx
import pytest
from evidence_websearch.domain.allowlist import Allowlist, DomainRule
from evidence_websearch.domain.fetcher import (
    FetchStatus,
    PageFetcher,
    UrlRejectedError,
    validate_url,
)

from evidence_models import WebTrustTier


def _rule(domain: str) -> DomainRule:
    return DomainRule(
        domain=domain,
        trust_tier=WebTrustTier.international_organization,
        status="enabled",
        is_default=True,
        metadata_only=False,
    )


ALLOWLIST = Allowlist(rules={"who.int": _rule("who.int"), "cdc.gov": _rule("cdc.gov")})


# (id, url, expected reason prefix). Reasons are asserted exactly because
# "rejected for some reason" is not the property under test — a probe that
# trips the wrong defense is a defense that will stop working when the URL
# changes slightly (an SSRF attempt refused for a bad PORT would sail through
# on :443).
ATTACKS: list[tuple[str, str, str]] = [
    ("aws_metadata_ip", "http://169.254.169.254/latest/meta-data/", "ip_literal_host"),
    ("loopback_ip", "http://127.0.0.1/admin", "ip_literal_host"),
    ("private_rfc1918", "http://10.0.0.5/internal", "ip_literal_host"),
    ("ipv6_loopback", "http://[::1]/_cluster/health", "ip_literal_host"),
    (
        "ipv6_mapped_private",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "ip_literal_host",
    ),
    ("file_scheme", "file:///etc/passwd", "scheme_not_allowed"),
    ("gopher_scheme", "gopher://who.int:70/_admin", "scheme_not_allowed"),
    # Rejected on the credentials, which is EARLIER than the allowlist check:
    # a URL whose authority is designed to be misread must not get as far as
    # being compared to anything.
    ("credentials_in_url", "https://who.int:pass@evil.example.com/x", "credentials_in_url"),
    ("lookalike_suffix", "https://evil-who.int.attacker.com/guideline", "not_allowlisted"),
    ("nonstandard_port", "https://who.int:9200/_cluster/health", "port_not_allowed"),
]


@pytest.mark.parametrize(("attack_id", "url", "reason"), ATTACKS, ids=[a[0] for a in ATTACKS])
def test_attack_url_is_rejected_with_a_specific_reason(
    attack_id: str, url: str, reason: str
) -> None:
    with pytest.raises(UrlRejectedError) as excinfo:
        validate_url(url, allowlist=ALLOWLIST, allow_http=True, resolve=False)
    assert excinfo.value.reason.startswith(reason), (
        f"{attack_id}: expected reason {reason!r}, got {excinfo.value.reason!r}"
    )


def test_allowlisted_url_passes_the_same_gauntlet() -> None:
    """The control: the defenses must not simply refuse everything."""
    normalized, rule = validate_url(
        "https://who.int/publications/i/item/9789240012345",
        allowlist=ALLOWLIST,
        allow_http=False,
        resolve=False,
    )
    assert rule.domain == "who.int"
    assert normalized.startswith("https://who.int/")


def test_subdomain_of_an_allowlisted_domain_passes() -> None:
    normalized, rule = validate_url(
        "https://iris.who.int/handle/10665/1", allowlist=ALLOWLIST, allow_http=False, resolve=False
    )
    assert rule.domain == "who.int"
    assert "iris.who.int" in normalized


def test_dns_answer_in_private_space_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS rebinding: the name is allowlisted, the address is not.

    This is the one attack the allowlist alone cannot see — it needs the
    resolution step.
    """
    import socket

    def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(UrlRejectedError) as excinfo:
        validate_url("https://who.int/x", allowlist=ALLOWLIST, allow_http=False, resolve=True)
    assert excinfo.value.reason == "private_address"


async def test_redirect_off_the_allowlist_ends_the_fetch() -> None:
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        if request.url.host == "who.int" and request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data/"},
            request=request,
        )

    fetcher = PageFetcher(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        allow_http=True,
        timeout_s=2.0,
        max_bytes=1024,
        max_redirects=3,
        robots_timeout_s=1.0,
        user_agent="evidentia-test/1.0",
        resolve_dns=False,
    )

    result = await fetcher.fetch("https://who.int/start", allowlist=ALLOWLIST)

    assert result.status is FetchStatus.error
    assert result.skip_reason == "redirect_ip_literal_host"
    assert not any("169.254.169.254" in hop for hop in hops), (
        "the fetcher followed the redirect before validating it"
    )


async def test_oversized_body_is_aborted_mid_stream() -> None:
    """A lying Content-Length must not defeat the cap."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            content=b"x" * 50_000,
            headers={"content-type": "text/html", "content-length": "10"},
            request=request,
        )

    fetcher = PageFetcher(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        allow_http=True,
        timeout_s=2.0,
        max_bytes=1024,
        max_redirects=1,
        robots_timeout_s=1.0,
        user_agent="evidentia-test/1.0",
        resolve_dns=False,
    )

    result = await fetcher.fetch("https://who.int/big", allowlist=ALLOWLIST)

    assert result.status is FetchStatus.error
    assert result.skip_reason == "too_large"


async def test_content_type_trap_is_refused() -> None:
    """A "page" that is really a zip/pdf/binary is not fetched into the
    extractor — content-type is an allowlist, not a denylist."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            content=b"PK\x03\x04",
            headers={"content-type": "application/zip"},
            request=request,
        )

    fetcher = PageFetcher(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        allow_http=True,
        timeout_s=2.0,
        max_bytes=4096,
        max_redirects=1,
        robots_timeout_s=1.0,
        user_agent="evidentia-test/1.0",
        resolve_dns=False,
    )

    result = await fetcher.fetch("https://who.int/archive", allowlist=ALLOWLIST)

    assert result.status is FetchStatus.error
    assert result.skip_reason == "content_type:application/zip"


async def test_robots_disallow_is_honoured_and_recorded() -> None:
    """TC-8: a robots-disallowed page is skipped, with the reason in the trace."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /private/\n",
                headers={"content-type": "text/plain"},
                request=request,
            )
        return httpx.Response(
            200,
            text="<html><body>secret</body></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    fetcher = PageFetcher(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        allow_http=True,
        timeout_s=2.0,
        max_bytes=4096,
        max_redirects=1,
        robots_timeout_s=1.0,
        user_agent="evidentia-test/1.0",
        resolve_dns=False,
    )

    blocked = await fetcher.fetch("https://who.int/private/page", allowlist=ALLOWLIST)
    allowed = await fetcher.fetch("https://who.int/public/page", allowlist=ALLOWLIST)

    assert blocked.status is FetchStatus.robots_skip
    assert blocked.skip_reason == "robots_disallow"
    assert blocked.robots_ok is False
    assert allowed.status is FetchStatus.ok
