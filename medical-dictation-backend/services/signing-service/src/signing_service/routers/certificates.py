"""GET /signing/certificates — list local-KEP certs (M1·B3).

There is no certificates table yet. This is the minimal contract-satisfying
implementation: derive the cert list from the configured local (ІІТ)
provider in the registry. Returns ``[]`` when no local provider is wired.

Follow-up (non-blocking): a real ``list_certificates()`` on ``IitProvider``
would replace the derived single entry with the actual smartcard/token certs.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from medical_kep import ProviderName
from pydantic import BaseModel, ConfigDict

from auth import Claims

from ..deps import get_state, requires

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signing", tags=["signing"])

# Local qualified-signature providers (smartcard/token КЕП) — DIIA is a
# remote cloud signature and MOCK is test-only, so neither is a "local cert".
_LOCAL_PROVIDERS = frozenset({ProviderName.IIT})


class CertificateInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ProviderName
    subject_cn: str | None = None
    issuer_cn: str | None = None
    serial: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    is_qualified: bool


@router.get("/certificates", response_model=list[CertificateInfo])
async def list_certificates(
    claims: Annotated[Claims, Depends(requires("report.write", "report"))],
) -> list[CertificateInfo]:
    state = get_state()
    return [
        CertificateInfo(provider=name, is_qualified=True)
        for name in state.providers.providers
        if name in _LOCAL_PROVIDERS
    ]
