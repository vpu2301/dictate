"""Preferences CRUD — always scoped to the CALLING user.

There is deliberately no `user_id` path parameter. Editing someone
else's notification preferences is not a feature; omitting the parameter
means the endpoint cannot be made to do it by a missing check.
"""

from __future__ import annotations

from datetime import time
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from auth import Claims
from db import tenant_connection
from notification_events import Category, EmailMode

from ..deps import get_state, requires
from ..domain import repository as repo
from ..domain.catalog import CATALOG
from ..domain.preferences import DEFAULT_TIMEZONE

router = APIRouter(prefix="/v1/notifications/preferences", tags=["notifications"])


class CategoryPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Category
    in_app_enabled: bool
    email_mode: EmailMode
    # Echoed so a client can render "(default)" without embedding a copy
    # of the catalog, which would drift.
    is_default: bool = False
    digest_eligible: bool = False


class QuietHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: time | None = None
    end: time | None = None

    @field_validator("end")
    @classmethod
    def _paired(cls, v: time | None, info) -> time | None:  # type: ignore[no-untyped-def]
        start = info.data.get("start")
        if (start is None) != (v is None):
            raise ValueError("quiet hours start and end must be set together")
        return v


class PreferencesView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[CategoryPreference]
    timezone: str
    quiet_hours: QuietHours
    digest_hour: int = Field(ge=0, le=23)


class PreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[CategoryPreference] = Field(default_factory=list)
    timezone: str = DEFAULT_TIMEZONE
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    digest_hour: int = Field(default=8, ge=0, le=23)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, v: str) -> str:
        # Validated on write, not on read: a rejected save tells the user
        # immediately, whereas a bad value discovered at send time would
        # silently fall back and mis-time every future email.
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError(f"unknown IANA timezone: {v!r}") from exc
        return v


@router.get("", response_model=PreferencesView, summary="Read your notification preferences")
async def get_preferences(
    claims: Annotated[Claims, Depends(requires("notification.read", "notification"))],
) -> PreferencesView:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        overrides = await repo.load_all_preferences(conn, user_id=claims.sub)
        settings = await repo.load_settings(conn, user_id=claims.sub)

    categories = []
    for category, spec in CATALOG.items():
        override = overrides.get(category)
        categories.append(
            CategoryPreference(
                category=category,
                in_app_enabled=override.in_app_enabled if override else spec.default_in_app,
                email_mode=override.email_mode if override else spec.default_email_mode,
                is_default=override is None,
                digest_eligible=spec.digest_eligible,
            )
        )

    return PreferencesView(
        categories=categories,
        timezone=settings.timezone,
        quiet_hours=QuietHours(
            start=settings.quiet_hours_start, end=settings.quiet_hours_end
        ),
        digest_hour=settings.digest_hour,
    )


@router.put("", response_model=PreferencesView, summary="Replace your preferences")
async def put_preferences(
    body: PreferencesUpdate,
    claims: Annotated[Claims, Depends(requires("notification.write", "notification"))],
) -> PreferencesView:
    state = get_state()

    seen: set[Category] = set()
    for entry in body.categories:
        if entry.category in seen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "duplicate_category",
                    "detail": f"{entry.category} listed twice",
                },
            )
        seen.add(entry.category)

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        for entry in body.categories:
            await repo.upsert_preference(
                conn,
                tenant_id=claims.tid,
                user_id=claims.sub,
                category=entry.category,
                in_app_enabled=entry.in_app_enabled,
                email_mode=entry.email_mode,
            )
        await repo.upsert_settings(
            conn,
            tenant_id=claims.tid,
            user_id=claims.sub,
            timezone=body.timezone,
            quiet_hours_start=body.quiet_hours.start,
            quiet_hours_end=body.quiet_hours.end,
            digest_hour=body.digest_hour,
        )

    return await get_preferences(claims)
