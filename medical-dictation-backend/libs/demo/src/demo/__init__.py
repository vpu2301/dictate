"""Sprint-07 demo-mode helpers: rate limits + abuse audit kinds.

Imported only by services running in the HF Space (env flag
``MDX_DEMO_MODE=true``). Production deployments do not load this
module.
"""

from demo.audit_kinds import DEMO_AUDIT_KINDS
from demo.rate_limit import (
    DemoRateLimiter,
    RateLimitBreach,
    RateLimitConfig,
)

__all__ = [
    "DEMO_AUDIT_KINDS",
    "DemoRateLimiter",
    "RateLimitBreach",
    "RateLimitConfig",
]
