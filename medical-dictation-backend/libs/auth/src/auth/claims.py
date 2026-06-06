"""Strict claims model. ``extra="forbid"`` is the defining contract.

If a token arrives with a claim we don't recognise — e.g. an attacker tries
to inject ``is_admin: true`` — Pydantic raises and ``MalformedClaimsError``
fires. That guarantees no service can be tricked by a claim it doesn't know
to check.

**Deviation from the sprint-02 spec.** The spec lists only the
security-bearing fields (``sub``, ``tid``, ``roles``, ``scope``, ``mfa``,
``sid``, ``iss``, ``aud``, ``exp``, ``iat``, ``nbf``). In practice Keycloak
always emits a handful of standard JOSE/OIDC fields that we don't act on
(``jti``, ``typ``, ``azp``, ``auth_time``, ``acr``, ``session_state``,
``allowed-origins``, plus profile/email-scope fields). We enumerate those
explicitly as Optional defaults so the allowlist remains explicit and
unknown claims still raise — preserving the spec's defence against
``is_admin``-style injection while not fighting the IdP.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Claims(BaseModel):
    """The exact set of claims a libs/auth-verified token may carry.

    Adding a new claim is a deliberate decision: bump the schema, update
    the realm protocol mappers, and add a test that the new claim is parsed
    correctly. Unknown claim names are rejected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    # ── Spec-required (security-bearing) ─────────────────────────────────
    sub: UUID
    tid: UUID
    roles: list[str]
    scope: str = ""
    mfa: bool = False
    sid: str
    iss: str
    aud: str | list[str]
    exp: int
    iat: int
    nbf: int | None = None

    # ── Standard Keycloak/OIDC built-ins, accepted and ignored ──────────
    jti: str | None = None
    typ: str | None = None
    azp: str | None = None
    auth_time: int | None = None
    acr: str | None = None
    session_state: str | None = None
    allowed_origins: list[str] | None = Field(default=None, alias="allowed-origins")

    # ── Profile/email scope claims (silently accepted when scope enabled) ─
    preferred_username: str | None = None
    name: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    email: str | None = None
    email_verified: bool | None = None

    # ── Keycloak role mappers we don't use (`roles` is the canonical one) ─
    realm_access: dict[str, object] | None = None
    resource_access: dict[str, object] | None = None
