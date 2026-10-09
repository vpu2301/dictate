"""In-perimeter URL assertion (deployment half of ADR-0044).

The jury/generation backend for PHI-derived candidates must be reachable
only inside the cluster/VPN. If the configured base URL points at a public
host, the pipeline refuses to start — a raise, not a warning log.

"Private" here is a static, deterministic judgement on the URL itself
(loopback, RFC1918/4193 literals, single-label hostnames à la
docker-compose, *.internal/*.local/*.svc/*.cluster.local). Deliberately no
DNS resolution: resolution results vary by environment and would turn a
security assertion into a network flake.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from corpus_forge.domain.jury import PHIBoundaryViolation

_PRIVATE_SUFFIXES = (".internal", ".local", ".svc", ".cluster.local", ".localdomain")


def is_private_host(host: str) -> bool:
    host = host.strip().lower().rstrip(".")
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass  # not an IP literal — judge the hostname shape
    if "." not in host:
        return True  # single-label: docker-compose / k8s service name
    return any(host.endswith(suffix) for suffix in _PRIVATE_SUFFIXES)


def assert_in_perimeter_url(url: str, *, purpose: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme not in ("http", "https") or not is_private_host(host):
        raise PHIBoundaryViolation(
            f"{purpose} URL {url!r} does not resolve to a private host — "
            "PHI-derived candidates may only be judged in-perimeter "
            "(ADR-0044; deployment plan: refuse to start, don't warn)"
        )
