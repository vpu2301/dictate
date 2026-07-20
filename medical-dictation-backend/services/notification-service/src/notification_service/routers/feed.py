"""The in-app feed: list, badge count, mark-read."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from auth import Claims
from db import tenant_connection

from ..deps import get_state, requires
from ..domain import repository as repo
from ..ws.fanout import publish_unread_changed

router = APIRouter(prefix="/v1/notifications", tags=["notifications"])

MAX_PAGE_SIZE = 100


class NotificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    category: str
    title: str
    body_text: str
    deep_link: str
    resource_type: str
    resource_id: UUID | None
    severity: str
    read_at: datetime | None
    created_at: datetime


class FeedPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[NotificationItem]
    # Opaque by design: clients must not construct or reason about it,
    # so the encoding stays free to change.
    next_cursor: str | None = None
    unread_count: int


class UnreadCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unread_count: int


class ReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updated: int
    unread_count: int


def _encode_cursor(created_at: datetime, row_id: UUID) -> str:
    raw = json.dumps({"c": created_at.isoformat(), "i": str(row_id)})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, UUID | None]:
    if not cursor:
        return None, None
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(raw["c"]), UUID(raw["i"])
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        # A bad cursor is a client error, not a silent reset to page 1 —
        # silently restarting would make a paging bug look like an
        # infinite feed.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "bad_cursor", "detail": "cursor is not valid"},
        ) from exc


@router.get("", response_model=FeedPage, summary="Cursor-paginated notification feed")
async def list_notifications(
    claims: Annotated[Claims, Depends(requires("notification.read", "notification"))],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
    unread_only: Annotated[bool, Query()] = False,
) -> FeedPage:
    state = get_state()
    before_created_at, before_id = _decode_cursor(cursor)

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await repo.list_feed(
            conn,
            user_id=claims.sub,
            limit=limit + 1,  # one extra row tells us whether more exist
            before_created_at=before_created_at,
            before_id=before_id,
            unread_only=unread_only,
        )
        count = await repo.unread_count(conn, user_id=claims.sub)

    has_more = len(rows) > limit
    page = rows[:limit]
    items = [NotificationItem(**dict(r)) for r in page]
    next_cursor = (
        _encode_cursor(page[-1]["created_at"], page[-1]["id"]) if has_more and page else None
    )
    return FeedPage(items=items, next_cursor=next_cursor, unread_count=count)


@router.get(
    "/unread-count",
    response_model=UnreadCount,
    summary="Cheap badge count",
)
async def get_unread_count(
    claims: Annotated[Claims, Depends(requires("notification.read", "notification"))],
) -> UnreadCount:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        return UnreadCount(unread_count=await repo.unread_count(conn, user_id=claims.sub))


@router.post(
    "/{notification_id}/read",
    response_model=ReadResult,
    summary="Mark one notification read (idempotent)",
)
async def mark_read(
    notification_id: UUID,
    claims: Annotated[Claims, Depends(requires("notification.write", "notification"))],
) -> ReadResult:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        # `read_at = coalesce(read_at, now())` — marking an already-read
        # row again succeeds and does not move the timestamp, so a
        # double-click is not an error and does not rewrite history.
        found = await repo.mark_read(
            conn, user_id=claims.sub, notification_id=notification_id
        )
        if not found:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "detail": "no such notification"},
            )
        count = await repo.unread_count(conn, user_id=claims.sub)

    await publish_unread_changed(
        state.redis, tenant_id=claims.tid, recipient_user_id=claims.sub
    )
    return ReadResult(updated=1, unread_count=count)


@router.post("/read-all", response_model=ReadResult, summary="Mark every notification read")
async def mark_all_read(
    claims: Annotated[Claims, Depends(requires("notification.write", "notification"))],
) -> ReadResult:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        updated = await repo.mark_all_read(conn, user_id=claims.sub)
        count = await repo.unread_count(conn, user_id=claims.sub)

    await publish_unread_changed(
        state.redis, tenant_id=claims.tid, recipient_user_id=claims.sub
    )
    return ReadResult(updated=updated, unread_count=count)
