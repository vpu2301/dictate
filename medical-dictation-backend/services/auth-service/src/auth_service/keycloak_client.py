"""Thin async wrapper over the Keycloak HTTP API.

Only the endpoints auth-service actually calls are exposed:

- :meth:`password_grant`  — POST /token with grant_type=password (mdx-backend secret)
- :meth:`refresh`         — POST /token with grant_type=refresh_token
- :meth:`logout`          — POST /logout to revoke a refresh
- :meth:`admin_token`     — internal: caches a service-account access token
- :meth:`create_user`     — POST /admin/realms/{realm}/users
- :meth:`logout_user`     — POST /admin/realms/{realm}/users/{sub}/logout (revoke all sessions)
- :meth:`set_user_enabled` — PUT /admin/realms/{realm}/users/{sub}

Errors are surfaced as :class:`KeycloakError` with the HTTP status and the
upstream body so callers can discriminate (e.g. 401 invalid creds vs 423
account locked).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)


class KeycloakError(Exception):
    """Wraps an unexpected Keycloak response. The ``status`` field carries
    the HTTP code; ``body`` is the parsed JSON or the raw text."""

    def __init__(self, *, status: int, body: Any, message: str | None = None) -> None:
        super().__init__(message or f"Keycloak responded {status}: {body!r}")
        self.status = status
        self.body = body


@dataclass(frozen=True, slots=True)
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int
    token_type: str


class KeycloakClient:
    """Async HTTP client for the medical-dictation realm."""

    def __init__(
        self,
        *,
        base_url: str,
        realm: str,
        login_client_id: str,
        login_client_secret: str,
        admin_client_id: str,
        admin_client_secret: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._realm = realm
        self._login_client_id = login_client_id
        self._login_client_secret = login_client_secret
        self._admin_client_id = admin_client_id
        self._admin_client_secret = admin_client_secret
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=10.0)
        self._admin_token: str | None = None
        self._admin_token_exp: float = 0.0
        self._admin_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ── URLs ─────────────────────────────────────────────────────────────
    @property
    def token_url(self) -> str:
        return f"{self._base}/realms/{self._realm}/protocol/openid-connect/token"

    @property
    def logout_url(self) -> str:
        return f"{self._base}/realms/{self._realm}/protocol/openid-connect/logout"

    def _admin_users_url(self, sub: str | None = None) -> str:
        base = f"{self._base}/admin/realms/{self._realm}/users"
        return f"{base}/{sub}" if sub else base

    # ── Token endpoints ─────────────────────────────────────────────────
    async def password_grant(self, *, username: str, password: str) -> TokenResponse:
        resp = await self._client.post(
            self.token_url,
            data={
                "grant_type": "password",
                "client_id": self._login_client_id,
                "client_secret": self._login_client_secret,
                "username": username,
                "password": password,
                "scope": "openid",
            },
        )
        return _parse_token(resp)

    async def refresh(self, *, refresh_token: str) -> TokenResponse:
        resp = await self._client.post(
            self.token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": self._login_client_id,
                "client_secret": self._login_client_secret,
                "refresh_token": refresh_token,
            },
        )
        return _parse_token(resp)

    async def logout(self, *, refresh_token: str) -> None:
        resp = await self._client.post(
            self.logout_url,
            data={
                "client_id": self._login_client_id,
                "client_secret": self._login_client_secret,
                "refresh_token": refresh_token,
            },
        )
        # 204 is success. Keycloak also returns 200 in some versions.
        if resp.status_code not in (200, 204):
            raise KeycloakError(status=resp.status_code, body=_body(resp))

    # ── Admin API ───────────────────────────────────────────────────────
    async def admin_token(self) -> str:
        """Return a cached admin access token, refreshing if near expiry."""
        async with self._admin_lock:
            now = time.monotonic()
            if self._admin_token is not None and now < self._admin_token_exp - 30:
                return self._admin_token

            resp = await self._client.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._admin_client_id,
                    "client_secret": self._admin_client_secret,
                },
            )
            tok = _parse_token(resp)
            self._admin_token = tok.access_token
            self._admin_token_exp = now + tok.expires_in
            return tok.access_token

    async def _admin_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.admin_token()}"}

    async def create_user(
        self,
        *,
        email: str,
        first_name: str,
        last_name: str,
        tenant_id: UUID,
        realm_role: str,
        send_invite_email: bool = False,
    ) -> UUID:
        """POST /admin/realms/{realm}/users — returns the new user's sub.

        We then GET the user back by username to get the assigned id.
        """
        username = email  # one-to-one with email in our model
        payload: dict[str, Any] = {
            "username": username,
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
            "enabled": True,
            "emailVerified": False,
            "attributes": {
                "tenant_id": [str(tenant_id)],
                "mfa_enrolled_at": [],
            },
            "requiredActions": ["UPDATE_PASSWORD"],
        }
        if send_invite_email:
            payload["requiredActions"].append("VERIFY_EMAIL")

        resp = await self._client.post(
            self._admin_users_url(),
            json=payload,
            headers=await self._admin_headers(),
        )
        if resp.status_code == 409:
            raise KeycloakError(
                status=409,
                body=_body(resp),
                message=f"user with email {email!r} already exists",
            )
        if resp.status_code != 201:
            raise KeycloakError(status=resp.status_code, body=_body(resp))

        # Keycloak returns 201 with a Location header containing the new id.
        location = resp.headers.get("Location") or ""
        sub_str = location.rsplit("/", 1)[-1] if location else ""
        if not sub_str:
            raise KeycloakError(
                status=201,
                body=_body(resp),
                message="Keycloak create_user did not return a Location header",
            )
        sub = UUID(sub_str)

        # Assign the realm role.
        await self._assign_realm_role(sub, realm_role)
        return sub

    async def _assign_realm_role(self, sub: UUID, role_name: str) -> None:
        # Fetch role representation first.
        role_url = f"{self._base}/admin/realms/{self._realm}/roles/{role_name}"
        role_resp = await self._client.get(role_url, headers=await self._admin_headers())
        if role_resp.status_code != 200:
            raise KeycloakError(status=role_resp.status_code, body=_body(role_resp))
        role = role_resp.json()

        # Assign.
        assign_url = f"{self._admin_users_url(str(sub))}/role-mappings/realm"
        ar = await self._client.post(
            assign_url, json=[role], headers=await self._admin_headers()
        )
        if ar.status_code not in (200, 204):
            raise KeycloakError(status=ar.status_code, body=_body(ar))

    async def logout_user(self, sub: UUID) -> None:
        """Revoke every active session for ``sub`` (admin operation)."""
        url = f"{self._admin_users_url(str(sub))}/logout"
        resp = await self._client.post(url, headers=await self._admin_headers())
        if resp.status_code not in (200, 204):
            raise KeycloakError(status=resp.status_code, body=_body(resp))

    async def set_user_enabled(self, sub: UUID, *, enabled: bool) -> None:
        url = self._admin_users_url(str(sub))
        resp = await self._client.put(
            url, json={"enabled": enabled}, headers=await self._admin_headers()
        )
        if resp.status_code not in (200, 204):
            raise KeycloakError(status=resp.status_code, body=_body(resp))


def _body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text


def _parse_token(resp: httpx.Response) -> TokenResponse:
    if resp.status_code == 200:
        d = resp.json()
        return TokenResponse(
            access_token=d["access_token"],
            refresh_token=d.get("refresh_token", ""),
            expires_in=int(d.get("expires_in", 0)),
            refresh_expires_in=int(d.get("refresh_expires_in", 0)),
            token_type=d.get("token_type", "Bearer"),
        )
    raise KeycloakError(status=resp.status_code, body=_body(resp))
