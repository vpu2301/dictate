"""The producer → consumer event envelope.

One domain fact, published once by whichever service owns the
transition. The consumer decides who (if anyone) hears about it; a
producer never names channels or renders text.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import Category

EVENT_SCHEMA_VERSION: Final = "1"

# A payload value must be a scalar. Nested structures are refused because
# they are the easy path to smuggling a whole report body — and therefore
# PHI — into an email template. Keeping the payload flat and small keeps
# the PHI boundary auditable by reading one dict.
_ALLOWED_PAYLOAD_TYPES: Final = (str, int, float, bool, type(None))
_MAX_PAYLOAD_KEYS: Final = 20
_MAX_PAYLOAD_VALUE_LEN: Final = 200


class NotificationEvent(BaseModel):
    """A domain fact worth telling someone about.

    ``extra="forbid"``: an unrecognised field means producer and consumer
    disagree about the contract, and silently dropping it would hide a
    real deploy-skew bug (ADR-0012 lineage).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The producer's idempotency seed. Re-publishing the same fact (retry,
    # replay, at-least-once redelivery) MUST reuse the same event_id — the
    # consumer derives each row's dedupe_key from it, so a duplicate
    # collapses onto the row it already wrote.
    event_id: UUID
    schema_version: str = EVENT_SCHEMA_VERSION

    tenant_id: UUID
    category: Category

    # Who caused the fact. Nullable because jobs (the chain reconciler)
    # act as the system, not as a user.
    actor_user_id: UUID | None = None

    resource_type: str = Field(min_length=1, max_length=64)
    resource_id: UUID
    resource_version_id: UUID | None = None

    occurred_at: datetime

    # Recipients the PRODUCER already knows (a report's author and
    # co-authors). Categories whose recipient rule is role-derived —
    # report.chain_failure fans out to tenant admins — leave this empty
    # and let the consumer resolve it; the producer has no business
    # querying the membership table.
    recipient_hints: tuple[UUID, ...] = ()

    # Flat, scalar-only, PHI-free. Carries pointers (a report code, a
    # provider name), never content.
    payload: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _require_tz(cls, v: datetime) -> datetime:
        # A naive timestamp silently means "server local time", which
        # breaks quiet-hours and digest windowing across deploys.
        if v.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        return v

    @field_validator("payload")
    @classmethod
    def _check_payload(
        cls, v: dict[str, str | int | float | bool | None]
    ) -> dict[str, str | int | float | bool | None]:
        if len(v) > _MAX_PAYLOAD_KEYS:
            raise ValueError(f"payload has {len(v)} keys; max is {_MAX_PAYLOAD_KEYS}")
        for key, value in v.items():
            if not isinstance(value, _ALLOWED_PAYLOAD_TYPES):
                raise ValueError(
                    f"payload[{key!r}] is {type(value).__name__}; "
                    "only scalars are allowed (see the PHI boundary, ADR-0031)"
                )
            if isinstance(value, str) and len(value) > _MAX_PAYLOAD_VALUE_LEN:
                raise ValueError(
                    f"payload[{key!r}] is {len(value)} chars; "
                    f"max is {_MAX_PAYLOAD_VALUE_LEN} — payloads carry pointers, not content"
                )
        return v

    def dedupe_key(self, recipient_user_id: UUID) -> str:
        """Stable idempotency anchor for one materialised row.

        Derived from the producer's event_id, so replaying the same event
        collapses onto the row already written rather than creating a
        second one. UNIQUE per tenant in the schema.
        """
        return f"{self.event_id}:{recipient_user_id}"
