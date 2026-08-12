"""Drain the account-mail outbox.

**One row per transaction.** This is the whole design note. Claiming a
batch inside a single transaction is the obvious implementation and it
loses mail: a failure on row N rolls back the already-recorded sends of
rows 1..N-1, so those messages are gone from the database but not from
the recipients' inboxes, and the next poll sends them again. That bug
was found and fixed in marketing-service; it is not being re-introduced
here, where a duplicate means a second live password-reset link.

The other half of the shape: because the outbox is RLS-scoped and this
loop has no tenant of its own, it asks the unscoped writer pool which
tenants have due mail and then opens a properly scoped connection per
tenant. The alternative — granting the worker a pool that bypasses RLS —
would put a permanent hole in the tenancy boundary for the sake of a
background job.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

import asyncpg

from db import tenant_connection

from ..adapters import templates
from ..adapters.email import (
    EmailPermanentError,
    EmailProvider,
    OutboundEmail,
)
from ..domain import compose
from ..domain import copy as copy_mod
from ..domain import repository as repo

logger = logging.getLogger(__name__)


def _as_dict(value: Any) -> dict[str, Any]:
    """asyncpg hands JSONB back as ``str`` unless a codec is registered."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return dict(json.loads(value))


async def deliver_one(
    conn: asyncpg.Connection,
    *,
    provider: EmailProvider,
    reply_to: str,
    max_attempts: int,
    backoff_base_s: float,
) -> bool:
    """Claim, render and send at most one mail. True if one was handled."""
    row = await repo.claim_due_mail(conn)
    if row is None:
        return False

    mail_id = UUID(str(row["id"]))
    kind = str(row["kind"])
    lang = copy_mod.normalise_lang(str(row["lang"]))
    fields = _as_dict(row["render_fields"])
    secrets = _as_dict(row["secret_fields"])
    attempt = int(row["attempt_count"])

    try:
        rendered = templates.render(
            kind,
            lang,
            subject=copy_mod.subject_for(kind, lang),
            text_body=copy_mod.text_body(
                kind, lang, compose.text_values(kind, lang, fields, secrets)
            ),
            context={**fields, **secrets},
        )
    except Exception as exc:  # noqa: BLE001
        # A render failure is deterministic — the same row will fail
        # identically forever. Retrying it would burn attempts and delay
        # every mail behind it, so it dead-letters immediately.
        logger.error(
            "auth.mail.render_failed",
            extra={"mail_id": str(mail_id), "kind": kind, "error": str(exc)},
        )
        await repo.mark_dead(conn, mail_id=mail_id, error=f"render: {exc}")
        return True

    try:
        result = await provider.send(
            OutboundEmail(
                to_address=str(row["to_address"]),
                subject=rendered.subject,
                text_body=rendered.text_body,
                html_body=rendered.html_body,
                reply_to=reply_to,
            )
        )
    except EmailPermanentError as exc:
        logger.error(
            "auth.mail.permanent_failure",
            extra={"mail_id": str(mail_id), "kind": kind, "error": str(exc)},
        )
        await repo.mark_dead(conn, mail_id=mail_id, error=str(exc))
        return True
    except Exception as exc:  # noqa: BLE001
        attempts_used = attempt + 1
        if attempts_used >= max_attempts:
            logger.error(
                "auth.mail.attempts_exhausted",
                extra={
                    "mail_id": str(mail_id),
                    "kind": kind,
                    "attempts": attempts_used,
                    "error": str(exc),
                },
            )
            await repo.mark_dead(conn, mail_id=mail_id, error=str(exc))
            return True
        delay = min(backoff_base_s * (2**attempt), 3600.0)
        logger.warning(
            "auth.mail.retry_scheduled",
            extra={
                "mail_id": str(mail_id),
                "kind": kind,
                "attempt": attempts_used,
                "delay_s": delay,
                "error": str(exc),
            },
        )
        await repo.mark_retry(
            conn, mail_id=mail_id, error=str(exc), delay_seconds=delay
        )
        return True

    await repo.mark_sent(
        conn, mail_id=mail_id, provider_message_id=result.provider_message_id
    )
    logger.info(
        "auth.mail.sent",
        extra={"mail_id": str(mail_id), "kind": kind, "lang": lang},
    )
    return True


async def deliver_once(
    *,
    app_pool: asyncpg.Pool,
    provider: EmailProvider,
    reply_to: str,
    batch_size: int,
    max_attempts: int,
    backoff_base_s: float,
) -> int:
    """One drain pass. Returns how many rows were handled."""
    # Unscoped app_role connection: the lookup is a SECURITY DEFINER
    # function, which is the only thing on this pool that can see across
    # tenants. Every row of actual work below runs scoped.
    async with app_pool.acquire() as conn:
        tenant_ids = await repo.tenants_with_due_mail(conn)
    if not tenant_ids:
        return 0

    handled = 0
    for tenant_id in tenant_ids:
        while handled < batch_size:
            # A fresh scoped connection — and therefore a fresh
            # transaction — per row. See the module docstring.
            async with tenant_connection(app_pool, tenant_id) as conn:
                did_work = await deliver_one(
                    conn,
                    provider=provider,
                    reply_to=reply_to,
                    max_attempts=max_attempts,
                    backoff_base_s=backoff_base_s,
                )
            if not did_work:
                break
            handled += 1
        if handled >= batch_size:
            break
    return handled


async def run_forever(
    *,
    app_pool: asyncpg.Pool,
    provider: EmailProvider,
    reply_to: str,
    interval_s: float,
    batch_size: int,
    max_attempts: int,
    backoff_base_s: float,
) -> None:
    logger.info("auth.mail.worker_started", extra={"interval_s": interval_s})
    while True:
        try:
            sent = await deliver_once(
                app_pool=app_pool,
                provider=provider,
                reply_to=reply_to,
                batch_size=batch_size,
                max_attempts=max_attempts,
                backoff_base_s=backoff_base_s,
            )
            # A full batch means there is probably more waiting; go
            # straight round again rather than sleeping on a backlog.
            if sent >= batch_size:
                continue
        except asyncio.CancelledError:
            logger.info("auth.mail.worker_stopped")
            raise
        except Exception as exc:  # noqa: BLE001
            # The loop must outlive any single failure — a worker that
            # dies on one bad poll stops all account mail until the next
            # deploy, and nothing else in the system would notice.
            logger.exception(
                "auth.mail.drain_failed", extra={"error": str(exc)}
            )
        await asyncio.sleep(interval_s)
