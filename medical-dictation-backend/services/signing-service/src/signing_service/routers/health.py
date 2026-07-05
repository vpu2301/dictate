"""Liveness/readiness routes."""

from __future__ import annotations

from fastapi import APIRouter

from ..deps import get_state

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, object]:
    state = get_state()
    return {
        "status": "ok",
        # Session providers (diia, mock, …) AND inline signers (file_key,
        # dev_password) — the frontend dialog offers only what's listed here.
        "providers": [
            *state.providers.providers.keys(),
            *state.providers.inline.keys(),
        ],
        "trust_anchors": len(state.trust_store.all_anchors()),
    }
