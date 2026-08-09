"""The last gate before a КЕП provider is touched (hotfix).

Every signing route in this service carries a
``requires("report.sign", "report")`` dependency, and that is the control
that should stop a non-clinician. This module is the one behind it.

The reason it exists is the shape of the defect it closes. Before the
hotfix, every route here gated on ``report.write`` — a permission nurses
legitimately hold, because they legitimately author reports. Nothing in
the service ever asked "may this principal *sign*?", so the question had
no answer anywhere near the crypto. A single wrong dependency on a single
new route was all it took to hand a qualified electronic signature to
whoever called it.

So the handlers assert it again, in the body, immediately before
dispatching to a provider:

    await assert_may_sign(claims, resource_kind="report")
    ...
    init = await provider.initiate(...)

Belt and braces on purpose. A route added later that forgets the
dependency still cannot reach `providers.py`, and the attempt is
recorded at ``sec`` severity naming the role and the principal — an
operational admin repeatedly trying to sign charts is exactly the
pattern a compliance review needs to see, and a silent 403 does not
produce it.
"""

from __future__ import annotations

import logging
from typing import Final

from fastapi import HTTPException, status
from opentelemetry import metrics

from audit import Severity
from auth import Claims, can_claims

from . import audit_kinds
from .deps import get_state

logger = logging.getLogger(__name__)

_meter = metrics.get_meter("mdx.signing.authority")
# The alertable signal. The audit chain is the record of record, but it is
# queried by a human after the fact; this is what makes "non-clinicians are
# hitting signing endpoints" visible without anyone going to look.
_denied_counter = _meter.create_counter(
    "mdx_signing_denied_total",
    description="Signing attempts refused for lack of report.sign, by role",
    unit="1",
)

# The matrix tuple that means "may apply a clinical signature". Kept next
# to the assertion rather than inlined at each call site so widening it is
# a single reviewable edit.
_SIGN_PERMISSION: Final[tuple[str, str]] = ("report.sign", "report")


def may_sign(claims: Claims) -> bool:
    """Predicate form — for handlers that branch rather than refuse."""
    return can_claims(claims, *_SIGN_PERMISSION)


async def assert_may_sign(claims: Claims, *, resource_kind: str = "report") -> None:
    """Refuse, audit, and raise 403 unless ``claims`` may sign.

    Call this BEFORE selecting or invoking a provider. It is deliberately
    async — the audit write is the point, and a synchronous variant would
    invite call sites that skip it.
    """
    if may_sign(claims):
        return

    _denied_counter.add(
        1,
        {
            "tenant_id": str(claims.tid),
            "role": (claims.roles[0] if claims.roles else "unknown"),
            "resource_kind": resource_kind,
        },
    )
    await _audit_denied(claims, resource_kind=resource_kind)

    exc = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "applying a qualified electronic signature to a clinical "
            "record is restricted to clinicians"
        ),
    )
    exc.problem_extras = {  # type: ignore[attr-defined]
        "code": "signing_not_permitted",
        "required_permission": _SIGN_PERMISSION[0],
        "resource_kind": resource_kind,
    }
    raise exc


async def _audit_denied(claims: Claims, *, resource_kind: str) -> None:
    """`sec` severity: a refused signing attempt is security-relevant.

    Best-effort, like every other audit write on a refusal path — the
    denial itself must not depend on the chain being reachable. The
    warning below is the operator's signal that one went unrecorded.
    """
    state = get_state()
    try:
        await state.audit_writer.write_event(
            tenant_id=claims.tid,
            kind=audit_kinds.SIGNING_DENIED_ROLE,
            actor_sub=claims.sub,
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind=resource_kind,
            target_id=claims.sub,
            payload={
                # The full role list, not just the primary: "which of this
                # person's roles did they think authorised this?" is the
                # first question a reviewer asks.
                "roles": list(claims.roles),
                "required_permission": _SIGN_PERMISSION[0],
                "resource_kind": resource_kind,
            },
            severity=Severity.SEC,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "signing.denied_role.audit_write_failed", extra={"error": str(exc)}
        )
