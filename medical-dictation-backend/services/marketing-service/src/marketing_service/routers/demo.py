"""The public demo-request endpoint, and the .ics the confirmation links to.

Unauthenticated by necessity — the caller is a visitor who has no
account and, if the demo goes well, may never need one. That makes
everything here attacker-reachable, so the surface is kept to two routes
and both are boring: one INSERT plus an outbox row, and one read of an
already-stored event.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from .. import metrics
from ..config import settings
from ..deps import get_state
from ..domain import compose
from ..domain import repository as repo
from ..domain.language import normalise_country, resolve_language

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/public/demo", tags=["public-demo"])

# Headers a CDN or ingress uses to report the client's country. Checked
# in order; the first present one wins.
_COUNTRY_HEADERS = ("CF-IPCountry", "X-Country-Code", "X-Geo-Country")


class DemoRequestIn(BaseModel):
    # extra="forbid": an unauthenticated JSON body is the one place where
    # silently accepting unknown keys is most likely to hide a client
    # sending something we never agreed to.
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    # Which of the two public forms this came from, and therefore which
    # acknowledgement to send. Absent means "demo" so an older SPA build
    # keeps working exactly as before — this field was added after the
    # demo flow shipped.
    kind: Literal["demo", "contact", "subscribe"] = "demo"
    # The UI language of the page the form was on. Optional because the
    # marketing site may not always send it; when absent the country and
    # Accept-Language take over (see domain/language.py).
    lang: str | None = Field(default=None, max_length=16)
    country: str | None = Field(default=None, max_length=8)
    first_name: str | None = Field(default=None, max_length=80)
    organisation: str | None = Field(default=None, max_length=160)
    source_page: str | None = Field(default=None, max_length=300)
    # The contact form's two extra fields. Optional for the same reason `kind`
    # is: a demo or subscribe submission has neither, and an older SPA build
    # that sends neither must keep working.
    #
    # 8000 is a cap, not a target. It is long enough that no genuine enquiry is
    # truncated and short enough that the sales notice stays a readable email
    # rather than a denial-of-service against a mailbox; the SPA trims to the
    # same number so the visitor is never silently 422'd on a long message.
    message: str | None = Field(default=None, max_length=8000)
    reason: str | None = Field(default=None, max_length=80)


class DemoRequestOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


def client_ip(request: Request) -> str:
    """Left-most X-Forwarded-For entry, else the socket peer.

    The header is client-controlled and therefore forgeable — which is
    fine for what it is used for here. A forged value spreads an
    attacker's own requests across buckets, i.e. it defeats their own
    rate limit and nobody else's; it cannot be used to exhaust a
    bystander's quota, because the quota is per-bucket, not global.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def hash_ip(ip: str) -> str | None:
    """Salted SHA-256. An unsalted IPv4 hash is brute-forceable in seconds."""
    if not ip:
        return None
    digest = hashlib.sha256(f"{settings.ip_hash_salt}:{ip}".encode())
    return digest.hexdigest()


def country_from(request: Request, body_country: str | None) -> str | None:
    """Edge header first, form field second.

    The edge sees where the connection actually came from; the form
    field is whatever the page decided to send, and on a static
    marketing site that is often a build-time constant.
    """
    for header in _COUNTRY_HEADERS:
        code = normalise_country(request.headers.get(header))
        if code:
            return code
    return normalise_country(body_country)


@router.post(
    "/requests",
    response_model=DemoRequestOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Record a demo request and queue the acknowledgement email",
)
async def create_request(body: DemoRequestIn, request: Request) -> DemoRequestOut:
    state = get_state()
    now = datetime.now(UTC)

    lang = resolve_language(
        form_lang=body.lang,
        country=country_from(request, body.country),
        accept_language=request.headers.get("Accept-Language"),
    )
    ip_hash = hash_ip(client_ip(request))

    async with state.app_pool.acquire() as conn, conn.transaction():
        if ip_hash:
            recent = await repo.requests_from_ip_since(
                conn, ip_hash=ip_hash, since=now - timedelta(hours=1)
            )
            if recent >= settings.rate_limit_per_ip_per_hour:
                metrics.demo_requests_rejected.add(1, {"reason": "rate_limit"})
                logger.warning("demo.rate_limited", extra={"ip_hash": ip_hash[:12]})
                # 202, not 429. The response is identical to the happy
                # path on purpose: a distinguishable rejection tells a
                # script exactly where the threshold is, and a real
                # visitor who double-submits gets a thank-you rather
                # than an error for something they did not do wrong.
                return DemoRequestOut(status="received")

        token = secrets.token_urlsafe(24)
        message = (body.message or "").strip()
        row = await repo.create_demo_request(
            conn,
            token=token,
            email=str(body.email),
            lang=lang,
            country=country_from(request, body.country),
            first_name=(body.first_name or "").strip() or None,
            organisation=(body.organisation or "").strip() or None,
            source_page=body.source_page,
            ip_hash=ip_hash,
            user_agent=(request.headers.get("User-Agent") or "")[:300] or None,
            message=message or None,
            reason=(body.reason or "").strip() or None,
        )

        # Two letters, one field apart. The contact form promises a human
        # reply and offers the calendar as the alternative to waiting; the
        # demo form sends people straight to it. Same variables either way,
        # so the compose call is shared and only the template differs.
        mail_kind = {
            "contact": "contact_received",
            "subscribe": "subscribe_confirmed",
        }.get(body.kind, "request_received")
        fields = compose.request_received_fields(
            lang=lang,
            email=str(body.email),
            first_name=body.first_name,
            token=token,
            requested_at=row["requested_at"],
            booking_url=settings.booking_url,
            public_base_url=settings.public_base_url,
            host_name=settings.host_name,
            demo_minutes=settings.demo_minutes,
        )
        await repo.enqueue_mail(
            conn,
            kind=mail_kind,
            to_address=str(body.email),
            lang=lang,
            render_fields=fields,
            request_id=row["id"],
        )

        # ── and a copy of the enquiry to a human ──────────────────────
        # The acknowledgement above promises the visitor that "a manager reads
        # it — not an autoresponder queue". Until this existed that was only
        # true if somebody was watching the CRM: the message went to HubSpot
        # from the browser and nowhere else. This is what makes the promise
        # structural rather than procedural.
        #
        # Conditions, all three required: it came from the contact form, it
        # actually carries a message, and a destination is configured. Blanking
        # MDX_MARKETING_SALES_NOTIFY_TO switches the forward off without
        # touching the acknowledgement the visitor gets.
        #
        # In the SAME transaction as the request row and the acknowledgement.
        # A notice that could be lost while the request commits is the failure
        # this whole outbox pattern exists to prevent.
        #
        # lang="en" is not the visitor's language: the letter is internal and
        # English-only (compose.contact_internal_fields says why). Their words
        # travel inside it untranslated.
        if body.kind == "contact" and message and settings.sales_notify_to:
            await repo.enqueue_mail(
                conn,
                kind="contact_internal",
                to_address=settings.sales_notify_to,
                lang="en",
                render_fields=compose.contact_internal_fields(
                    email=str(body.email),
                    first_name=body.first_name,
                    organisation=body.organisation,
                    message=message,
                    reason=body.reason,
                    form_lang=lang,
                    requested_at=row["requested_at"],
                    source_page=body.source_page,
                    public_base_url=settings.public_base_url,
                ),
                request_id=row["id"],
            )

    metrics.demo_requests.add(1, {"lang": lang, "kind": body.kind})
    logger.info(
        "demo.request_received",
        extra={"lang": lang, "kind": body.kind, "request_id": str(row["id"])},
    )
    return DemoRequestOut(status="received")


@router.get(
    "/{token}.ics",
    response_class=PlainTextResponse,
    summary="The calendar file the confirmation email links to",
)
async def booking_ics(token: str) -> Response:
    """Serve the saved booking as a calendar file.

    Keyed by the request token — which is 24 random bytes and appears
    only in that prospect's own email — so there is nothing to enumerate
    and no authentication to add.
    """
    state = get_state()
    async with state.app_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT b.ical_uid, b.starts_at, b.ends_at, b.meet_url, b.summary
            FROM marketing.demo_bookings b
            JOIN marketing.demo_requests r ON r.id = b.request_id
            WHERE r.token = $1 AND b.cancelled = FALSE
            ORDER BY b.sequence DESC, b.detected_at DESC
            LIMIT 1
            """,
            token,
        )

    if row is None:
        return PlainTextResponse("not found", status_code=status.HTTP_404_NOT_FOUND)

    body = compose.build_ics(
        uid=row["ical_uid"],
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
        meet_url=row["meet_url"] or "",
        organizer=settings.email_reply_to,
        stamp=datetime.now(UTC),
        summary=row["summary"] or "Klarnote — product demo",
    )
    return PlainTextResponse(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="klarnote-demo.ics"'},
    )
