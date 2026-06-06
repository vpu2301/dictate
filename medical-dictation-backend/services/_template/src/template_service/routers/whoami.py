"""Example protected route. Confirms libs/auth wires correctly.

``GET /whoami`` returns the verified claims for the bearer token. Sprint 02
uses this endpoint as the integration sanity check ("can a real Keycloak
token authenticate against a real service?").
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from auth import Claims

from ..main_deps import current_user

router = APIRouter(prefix="/whoami", tags=["whoami"])


@router.get("", summary="Return the calling user's verified claims")
async def whoami(claims: Annotated[Claims, Depends(current_user)]) -> dict[str, object]:
    return {
        "sub": str(claims.sub),
        "tid": str(claims.tid),
        "roles": claims.roles,
        "scope": claims.scope,
        "mfa": claims.mfa,
        "iss": claims.iss,
    }
