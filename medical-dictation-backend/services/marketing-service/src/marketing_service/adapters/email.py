"""Outbound mail.

A service-local copy of the provider shape notification-service uses,
rather than an import of it: "services may not import other services" is
an enforced contract, and the two have genuinely diverged — this one
sends from a human mailbox to strangers, so it needs Reply-To, a real
Message-ID and an implicit-TLS mode that the internal notifier does not.

The mock REFUSES to run in production, for the reason spelled out in the
sibling service: a mock that silently accepts mail in production looks
exactly like a working system while every send is discarded, and no
metric tells the two apart.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Final

logger = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    """Transient failure — the caller should retry with backoff."""


class EmailPermanentError(Exception):
    """The message will never be deliverable. Do not retry."""


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    to_address: str
    subject: str
    text_body: str
    html_body: str = ""
    reply_to: str = ""
    # The List-Unsubscribe value: an https:// URL or a mailto:. Gmail and
    # Yahoo require a working unsubscribe path on bulk mail; these two are
    # transactional and should never be filed as bulk, but the header costs
    # nothing and it is the difference between a spam report and a clean
    # opt-out.
    list_unsubscribe: str = ""
    # Whether to ALSO claim RFC 8058 one-click. Separate from the value
    # because the two are not the same promise: the header above says "here
    # is where to unsubscribe", the one below says "POST to it and we will
    # act on it unattended". Claiming the second with nothing serving the URL
    # is worse than claiming neither — see settings.unsubscribe_one_click.
    list_unsubscribe_one_click: bool = False
    attachments: tuple[tuple[str, str, str], ...] = field(default=())
    """(filename, mime_subtype, utf-8 text content) — used for the .ics."""
    inline_images: tuple[tuple[str, str, bytes], ...] = field(default=())
    """(content-id, filename, PNG bytes) for the `cid:` marks in the HTML.

    Related parts, not attachments: they belong TO the HTML body and must not
    appear as files hanging off the letter. See build_mime for the structure
    that distinction requires.
    """


@dataclass(frozen=True, slots=True)
class SendResult:
    provider_message_id: str


class EmailProvider(ABC):
    @abstractmethod
    async def send(self, message: OutboundEmail) -> SendResult: ...

    @abstractmethod
    async def aclose(self) -> None: ...


def build_mime(
    message: OutboundEmail,
    *,
    from_address: str,
    from_name: str,
    message_id: str | None = None,
    date: str | None = None,
) -> EmailMessage:
    """Assemble the MIME document.

    `message_id` and `date` are injectable so the round-trip test can
    assert on exact bytes; left alone they are generated, because a mail
    without either is scored as suspicious by every major provider.
    """
    mime = EmailMessage()
    mime["From"] = f"{from_name} <{from_address}>" if from_name else from_address
    mime["To"] = message.to_address
    mime["Subject"] = message.subject
    mime["Message-ID"] = message_id or make_msgid(domain=from_address.rsplit("@", 1)[-1])
    mime["Date"] = date or formatdate(localtime=True)
    if message.reply_to:
        mime["Reply-To"] = message.reply_to
    if message.list_unsubscribe:
        mime["List-Unsubscribe"] = f"<{message.list_unsubscribe}>"
        if message.list_unsubscribe_one_click:
            mime["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    # NOT Auto-Submitted: these are a direct reply to something a person
    # just did, and we WANT them to reply. Marking them auto-generated
    # tells the receiving server to suppress their out-of-office and
    # nudges some filters towards the promotions tab.
    mime.set_content(message.text_body)
    if message.html_body:
        mime.add_alternative(message.html_body, subtype="html")

        # The `cid:` marks go INSIDE the html alternative, as related parts —
        # multipart/alternative[ text , multipart/related[ html , png … ] ].
        # Hung off the top level instead they would be siblings of the text
        # part, which makes them attachments: the reader sees two mystery
        # files clipped to the letter, and a client that prefers the plain
        # text alternative shows them with no HTML to belong to.
        if message.inline_images:
            html_part = mime.get_payload()[-1]
            for content_id, filename, data in message.inline_images:
                html_part.add_related(
                    data,
                    maintype="image",
                    subtype="png",
                    cid=f"<{content_id}>",
                    filename=filename,
                    # Inline, not attachment: the same distinction again, this
                    # time for clients that go by the disposition rather than
                    # by the part tree.
                    disposition="inline",
                )

    for filename, subtype, content in message.attachments:
        mime.add_attachment(
            content.encode("utf-8"),
            maintype="text",
            subtype=subtype,
            filename=filename,
        )
    return mime


def ehlo_hostname(from_address: str) -> str:
    """The name we announce in EHLO. Never `socket.getfqdn()`.

    Left to itself, aiosmtplib resolves the local FQDN for the EHLO
    greeting — and `socket.getfqdn()` does a reverse DNS lookup that
    BLOCKS. On a laptop behind a consumer router with no PTR record that
    is a flat 30 seconds per message, inside the send, inside the
    transaction that holds the outbox row; measured, not theorised.
    Containers usually resolve instantly, which is exactly what makes it
    a bad thing to depend on: the stall shows up on somebody's machine
    or in one network and nowhere else.

    The sending domain is the right answer anyway. It is stable, it
    matches the envelope sender, and a receiving MTA that compares the
    two prefers it over a random container hostname.
    """
    domain = from_address.rsplit("@", 1)[-1].strip()
    return domain or "localhost"


class SmtpProvider(EmailProvider):
    """aiosmtplib against a relay — Mailpit in dev, Google Workspace in prod."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        from_address: str,
        from_name: str = "",
        use_tls: bool = False,
        username: str = "",
        password: str = "",
        timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._from_address = from_address
        self._from_name = from_name
        self._use_tls = use_tls
        self._username = username
        self._password = password
        self._timeout = timeout
        self._local_hostname = ehlo_hostname(from_address)

    async def send(self, message: OutboundEmail) -> SendResult:
        import aiosmtplib

        mime = build_mime(
            message, from_address=self._from_address, from_name=self._from_name
        )
        # Port 465 is implicit TLS (the socket is wrapped before the
        # greeting); everything else negotiates STARTTLS. Passing
        # start_tls=True on 465 makes aiosmtplib try to upgrade an
        # already-encrypted connection and the send fails with a
        # confusing protocol error rather than a credentials one.
        implicit_tls = self._port == 465
        try:
            await aiosmtplib.send(
                mime,
                hostname=self._host,
                port=self._port,
                use_tls=implicit_tls,
                start_tls=True if (self._use_tls and not implicit_tls) else None,
                username=self._username or None,
                password=self._password or None,
                timeout=self._timeout,
                local_hostname=self._local_hostname,
            )
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", None)
            # 5xx is a permanent refusal — an unknown mailbox, or an
            # authentication rejection. Retrying either burns attempts
            # and, on a shared relay, reputation.
            if isinstance(code, int) and 500 <= code < 600:
                raise EmailPermanentError(f"smtp permanent {code}: {exc}") from exc
            raise EmailDeliveryError(f"smtp failure: {exc}") from exc

        return SendResult(provider_message_id=str(mime.get("Message-ID") or ""))

    async def aclose(self) -> None:
        # aiosmtplib.send() opens and closes a connection per call.
        return None


class MockProvider(EmailProvider):
    """Captures mail in memory. Refuses to exist in production."""

    def __init__(self, *, is_production: bool = False) -> None:
        if is_production:
            raise RuntimeError(
                "MockProvider must never be used in production — demo mail would "
                "be silently discarded while every metric reported success. "
                "Set MDX_EMAIL_PROVIDER=smtp."
            )
        self.sent: list[OutboundEmail] = []

    async def send(self, message: OutboundEmail) -> SendResult:
        self.sent.append(message)
        logger.info(
            "marketing.email.mock_send",
            extra={"to": message.to_address, "subject": message.subject},
        )
        return SendResult(provider_message_id=f"<mock-{len(self.sent)}@klarnote.local>")

    async def aclose(self) -> None:
        return None


_SMTP: Final = "smtp"
_MOCK: Final = "mock"


def build_provider(
    *,
    kind: str,
    is_production: bool,
    host: str = "",
    port: int = 25,
    from_address: str = "",
    from_name: str = "",
    use_tls: bool = False,
    username: str = "",
    password: str = "",
) -> EmailProvider:
    if kind == _SMTP:
        return SmtpProvider(
            host=host,
            port=port,
            from_address=from_address,
            from_name=from_name,
            use_tls=use_tls,
            username=username,
            password=password,
        )
    if kind == _MOCK:
        return MockProvider(is_production=is_production)
    raise ValueError(f"unknown email provider {kind!r}; expected 'smtp' or 'mock'")
