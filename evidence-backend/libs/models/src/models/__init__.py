"""Gateway client — the only sanctioned model access path (rule E12).

Callers know ROLE NAMES, never model names; pins live in docs/models/PINS.md.
A direct HTTP call to a model endpoint anywhere else fails the
`check-no-direct-model` gate.
"""

from .client import (
    GatewayError,
    GatewayUnavailableError,
    GenerationResult,
    ModelGatewayClient,
)

__all__ = [
    "GatewayError",
    "GatewayUnavailableError",
    "GenerationResult",
    "ModelGatewayClient",
]
