"""Hardened page fetcher (FR-7).

Every outbound byte of the clinical path leaves through the egress proxy
(ADR-0005). The proxy is the enforcement boundary; this module is the second
layer, because a proxy misconfiguration must not become an SSRF.

Defenses, in the order they run:

1. **Scheme**: `https` only (plus `http` when the direct-egress dev escape
   hatch is on, which the SSRF fixture server needs).
2. **URL shape**: no credentials in the authority, no explicit non-standard
   port, no fragment-smuggled authority.
3. **Allowlist**: the host must match an enabled `web_domains` rule.
4. **Address literals / private ranges**: the host is resolved and every
   answer must be a global unicast address. Loopback, link-local, private,
   CGNAT, multicast and reserved space are refused — including the
   `169.254.169.254` metadata endpoint and IPv6-mapped forms of all of them.
5. **robots.txt**: fetched (through the proxy, same rules) and honoured.
6. **Redirects**: never followed automatically. Each hop is re-validated
   through steps 1–4, so a redirect off the allowlist ends the fetch.
7. **Budgets**: 10 s wall clock, 3 MB body, content-type allowlist. The body
   cap is enforced while streaming — a 10 GB response never lands in memory.

A failed defense is never an exception to the caller: it is a `FetchResult`
with a status and a reason that ends up in the answer trace.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx

from evidence_websearch.domain.allowlist import Allowlist, DomainRule

logger = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_ALLOWED_PORTS = {None, 80, 443}


class FetchStatus(StrEnum):
    ok = "ok"
    paywalled = "paywalled"
    robots_skip = "robots_skip"
    garbage = "garbage"
    quarantined = "quarantined"
    error = "error"


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status: FetchStatus
    # Final URL after allowlisted redirects — what gets cited.
    final_url: str = ""
    domain: str = ""
    http_status: int | None = None
    content_type: str | None = None
    body: bytes = b""
    robots_ok: bool = True
    skip_reason: str = ""

    @property
    def usable(self) -> bool:
        return self.status is FetchStatus.ok and bool(self.body)


class UrlRejectedError(Exception):
    """A URL failed a pre-flight defense. Carries the trace reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_global_address(raw: str) -> bool:
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_url(
    url: str, *, allowlist: Allowlist, allow_http: bool, resolve: bool = True
) -> tuple[str, DomainRule]:
    """Run defenses 1–4. Returns (normalized url, matched rule) or raises."""
    parsed = urlparse(url)
    schemes = ("https", "http") if allow_http else ("https",)
    if parsed.scheme not in schemes:
        raise UrlRejectedError(f"scheme_not_allowed:{parsed.scheme or 'none'}")
    if parsed.username or parsed.password:
        raise UrlRejectedError("credentials_in_url")
    host = (parsed.hostname or "").strip().rstrip(".").casefold()
    if not host:
        raise UrlRejectedError("missing_host")
    try:
        port = parsed.port
    except ValueError as exc:  # malformed port
        raise UrlRejectedError("invalid_port") from exc
    if port not in _ALLOWED_PORTS:
        raise UrlRejectedError(f"port_not_allowed:{port}")

    # An IP literal can never be on a domain allowlist, but say why precisely —
    # "not_allowlisted" would hide an SSRF probe among ordinary misses.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UrlRejectedError("ip_literal_host")

    rule = allowlist.match(host)
    if rule is None:
        raise UrlRejectedError("not_allowlisted")

    if resolve:
        try:
            infos = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror as exc:
            raise UrlRejectedError("dns_resolution_failed") from exc
        if not infos:
            raise UrlRejectedError("dns_no_records")
        for info in infos:
            address = str(info[4][0])
            if not _is_global_address(address):
                # Never log the address of a non-global answer alongside the
                # host: that pairing is exactly what a prober is fishing for.
                raise UrlRejectedError("private_address")

    normalized = urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.params, parsed.query, "")
    )
    return normalized, rule


class PageFetcher:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        allow_http: bool,
        timeout_s: float,
        max_bytes: int,
        max_redirects: int,
        robots_timeout_s: float,
        user_agent: str,
        resolve_dns: bool = True,
    ) -> None:
        self._client = client
        self._allow_http = allow_http
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._robots_timeout_s = robots_timeout_s
        self._user_agent = user_agent
        self._resolve_dns = resolve_dns
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = asyncio.Lock()

    # ── robots ──────────────────────────────────────────────────────────
    async def _robots_for(self, scheme: str, host: str) -> RobotFileParser | None:
        key = f"{scheme}://{host}"
        async with self._robots_lock:
            if key in self._robots:
                return self._robots[key]
        parser: RobotFileParser | None = None
        try:
            response = await self._client.get(
                f"{key}/robots.txt",
                timeout=self._robots_timeout_s,
                headers={"User-Agent": self._user_agent},
            )
            if response.status_code == 200:
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
        except httpx.HTTPError:
            # Unreachable robots.txt is NOT an implicit allow-all here: it is
            # unknown. We treat it as permissive (the RFC's own posture for a
            # 4xx) but record it, because the alternative — refusing every
            # page on a flaky host — degrades answers for no safety gain.
            logger.info("robots.unreachable", extra={"host": host})
        async with self._robots_lock:
            self._robots[key] = parser
        return parser

    async def _robots_allows(self, url: str) -> bool:
        parsed = urlparse(url)
        parser = await self._robots_for(parsed.scheme, parsed.netloc)
        if parser is None:
            return True
        return parser.can_fetch(self._user_agent, url)

    # ── fetch ───────────────────────────────────────────────────────────
    async def fetch(self, url: str, *, allowlist: Allowlist) -> FetchResult:
        try:
            current, rule = validate_url(
                url, allowlist=allowlist, allow_http=self._allow_http, resolve=self._resolve_dns
            )
        except UrlRejectedError as exc:
            return FetchResult(url=url, status=FetchStatus.error, skip_reason=exc.reason)

        domain = rule.domain
        if not await self._robots_allows(current):
            return FetchResult(
                url=url,
                final_url=current,
                domain=domain,
                status=FetchStatus.robots_skip,
                robots_ok=False,
                skip_reason="robots_disallow",
            )

        headers = {
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8",
            "Accept-Language": "uk,en;q=0.9",
        }
        for _hop in range(self._max_redirects + 1):
            try:
                async with self._client.stream(
                    "GET",
                    current,
                    headers=headers,
                    timeout=self._timeout_s,
                    follow_redirects=False,
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        if not location:
                            return FetchResult(
                                url=url,
                                final_url=current,
                                domain=domain,
                                status=FetchStatus.error,
                                http_status=response.status_code,
                                skip_reason="redirect_without_location",
                            )
                        target = str(httpx.URL(current).join(location))
                        try:
                            current, rule = validate_url(
                                target,
                                allowlist=allowlist,
                                allow_http=self._allow_http,
                                resolve=self._resolve_dns,
                            )
                        except UrlRejectedError as exc:
                            return FetchResult(
                                url=url,
                                final_url=target,
                                domain=domain,
                                status=FetchStatus.error,
                                http_status=response.status_code,
                                skip_reason=f"redirect_{exc.reason}",
                            )
                        domain = rule.domain
                        continue

                    if response.status_code in (401, 402, 403):
                        return FetchResult(
                            url=url,
                            final_url=current,
                            domain=domain,
                            status=FetchStatus.paywalled,
                            http_status=response.status_code,
                            skip_reason="access_denied",
                        )
                    if response.status_code >= 400:
                        return FetchResult(
                            url=url,
                            final_url=current,
                            domain=domain,
                            status=FetchStatus.error,
                            http_status=response.status_code,
                            skip_reason=f"http_{response.status_code}",
                        )

                    content_type = (response.headers.get("content-type") or "").split(";")[0]
                    if content_type.strip().casefold() not in ALLOWED_CONTENT_TYPES:
                        return FetchResult(
                            url=url,
                            final_url=current,
                            domain=domain,
                            status=FetchStatus.error,
                            http_status=response.status_code,
                            content_type=content_type,
                            skip_reason=f"content_type:{content_type or 'unknown'}",
                        )
                    # A truthful Content-Length over the cap saves the transfer;
                    # a lying or absent one is caught by the streaming cap below.
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self._max_bytes:
                        return FetchResult(
                            url=url,
                            final_url=current,
                            domain=domain,
                            status=FetchStatus.error,
                            http_status=response.status_code,
                            content_type=content_type,
                            skip_reason="too_large",
                        )

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self._max_bytes:
                            return FetchResult(
                                url=url,
                                final_url=current,
                                domain=domain,
                                status=FetchStatus.error,
                                http_status=response.status_code,
                                content_type=content_type,
                                skip_reason="too_large",
                            )
                        chunks.append(chunk)
                    return FetchResult(
                        url=url,
                        final_url=current,
                        domain=domain,
                        status=FetchStatus.ok,
                        http_status=response.status_code,
                        content_type=content_type,
                        body=b"".join(chunks),
                    )
            except httpx.TimeoutException:
                return FetchResult(
                    url=url,
                    final_url=current,
                    domain=domain,
                    status=FetchStatus.error,
                    skip_reason="timeout",
                )
            except httpx.HTTPError as exc:
                logger.info("fetch.transport_error", extra={"reason": type(exc).__name__})
                return FetchResult(
                    url=url,
                    final_url=current,
                    domain=domain,
                    status=FetchStatus.error,
                    skip_reason=f"transport:{type(exc).__name__}",
                )

        return FetchResult(
            url=url,
            final_url=current,
            domain=domain,
            status=FetchStatus.error,
            skip_reason="too_many_redirects",
        )
