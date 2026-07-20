"""Email provider abstraction.

Mirrors the sprint-09 ``SigningProvider`` split: one ABC, a real
implementation, and a mock that REFUSES TO RUN IN PRODUCTION. The
refusal is the point — a mock that silently accepts mail in production
looks exactly like a working system while every notification is
discarded, and nothing in the metrics distinguishes the two.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Final

logger = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    """Transient failure — the caller should retry with backoff."""


class EmailPermanentError(Exception):
    """The message will never be deliverable (bad address). Do not retry."""


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    to_address: str
    subject: str
    text_body: str
    html_body: str = ""


@dataclass(frozen=True, slots=True)
class SendResult:
    provider_message_id: str


class EmailProvider(ABC):
    """One method. Adding a channel (sprint 18 push) copies this shape."""

    @abstractmethod
    async def send(self, message: OutboundEmail) -> SendResult: ...

    @abstractmethod
    async def aclose(self) -> None: ...


def _build_mime(
    message: OutboundEmail, *, from_address: str, from_name: str
) -> EmailMessage:
    mime = EmailMessage()
    mime["From"] = f"{from_name} <{from_address}>" if from_name else from_address
    mime["To"] = message.to_address
    mime["Subject"] = message.subject
    # Marks the mail as automatic so recipients' out-of-office replies do
    # not bounce back into the noreply mailbox.
    mime["Auto-Submitted"] = "auto-generated"
    mime.set_content(message.text_body)
    if message.html_body:
        mime.add_alternative(message.html_body, subtype="html")
    return mime


class SmtpProvider(EmailProvider):
    """aiosmtplib against a relay (Mailpit in dev, a real relay in prod)."""

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
        timeout: float = 15.0,
    ) -> None:
        self._host = host
        self._port = port
        self._from_address = from_address
        self._from_name = from_name
        self._use_tls = use_tls
        self._username = username
        self._password = password
        self._timeout = timeout

    async def send(self, message: OutboundEmail) -> SendResult:
        import aiosmtplib

        mime = _build_mime(
            message, from_address=self._from_address, from_name=self._from_name
        )
        try:
            await aiosmtplib.send(
                mime,
                hostname=self._host,
                port=self._port,
                start_tls=self._use_tls or None,
                username=self._username or None,
                password=self._password or None,
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", None)
            # 5xx is a permanent refusal (unknown mailbox); retrying it
            # burns attempts and, on some relays, reputation. 4xx and
            # connection errors are transient.
            if isinstance(code, int) and 500 <= code < 600:
                raise EmailPermanentError(f"smtp permanent {code}: {exc}") from exc
            raise EmailDeliveryError(f"smtp failure: {exc}") from exc

        message_id = mime.get("Message-ID") or ""
        return SendResult(provider_message_id=str(message_id))

    async def aclose(self) -> None:
        # aiosmtplib.send() opens and closes a connection per call, so there
        # is no pooled client to tear down here.
        return None


class MockProvider(EmailProvider):
    """Captures mail in memory. Refuses to exist in production."""

    def __init__(self, *, is_production: bool = False) -> None:
        if is_production:
            raise RuntimeError(
                "MockProvider must never be used in production — mail would be "
                "silently discarded while every metric reported success. "
                "Set MDX_EMAIL_PROVIDER=smtp."
            )
        self.sent: list[OutboundEmail] = []

    async def send(self, message: OutboundEmail) -> SendResult:
        self.sent.append(message)
        logger.info(
            "email.mock_send",
            extra={"to": message.to_address, "subject": message.subject},
        )
        return SendResult(provider_message_id=f"mock-{len(self.sent)}")

    async def aclose(self) -> None:
        return None


class FailingProvider(EmailProvider):
    """Fails its first N sends, then succeeds. Drives the retry/DLQ demo."""

    def __init__(self, *, fail_times: int) -> None:
        self.remaining_failures = fail_times
        self.attempts = 0
        self.sent: list[OutboundEmail] = []

    async def send(self, message: OutboundEmail) -> SendResult:
        self.attempts += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise EmailDeliveryError(f"synthetic failure #{self.attempts}")
        self.sent.append(message)
        return SendResult(provider_message_id=f"late-{self.attempts}")

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
