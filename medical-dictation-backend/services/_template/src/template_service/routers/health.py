"""Liveness and readiness endpoints.

Naming follows the Kubernetes convention (``/healthz``, ``/readyz``) so the
manifest can wire ``livenessProbe`` and ``readinessProbe`` without aliasing.
"""

from fastapi import APIRouter, status
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


@router.get(
    "/healthz",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Liveness probe",
    description="Returns 200 when the process is alive.",
)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/readyz",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Readiness probe",
    description="Returns 200 when the service can accept traffic. "
    "Sprint 02+ extends this to verify DB and downstream dependencies.",
)
async def readyz() -> HealthResponse:
    return HealthResponse(status="ready")
