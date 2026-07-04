"""Keycloak password re-auth for the dev_password provider.

Performs the SAME resource-owner-password grant the auth-service login
uses (same realm, same confidential client), so Keycloak's brute-force
lockout and password policy apply unchanged. We never see, store, or
compare password hashes; the password goes to Keycloak over the local
network and the returned tokens are discarded immediately — the only
output is ok / wrong / locked / unavailable.
"""

from __future__ import annotations

import contextlib
import logging

import httpx
from medical_kep import PasswordCheckResult

from .config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


class KeycloakPasswordVerifier:
    """Async callable matching :data:`medical_kep.PasswordVerifier`."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        self._token_url = (
            settings.auth_issuer.rstrip("/") + "/protocol/openid-connect/token"
        )

    async def __call__(self, username: str, password: str) -> PasswordCheckResult:
        try:
            resp = await self._client.post(
                self._token_url,
                data={
                    "grant_type": "password",
                    "client_id": settings.keycloak_client_id,
                    "client_secret": settings.keycloak_client_secret,
                    "username": username,
                    "password": password,
                    "scope": "openid",
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("dev_password.keycloak_unreachable: %s", exc.__class__.__name__)
            return PasswordCheckResult.UNAVAILABLE

        if resp.status_code == 200:
            return PasswordCheckResult.OK
        if resp.status_code in (400, 401):
            # Keycloak reports both wrong credentials and temporary
            # lockout as invalid_grant; the description distinguishes.
            description = ""
            with contextlib.suppress(Exception):
                description = str(resp.json().get("error_description", ""))
            if "disabled" in description.lower() or "locked" in description.lower():
                return PasswordCheckResult.LOCKED
            return PasswordCheckResult.WRONG_PASSWORD
        logger.warning("dev_password.keycloak_unexpected_status: %s", resp.status_code)
        return PasswordCheckResult.UNAVAILABLE

    async def aclose(self) -> None:
        await self._client.aclose()
