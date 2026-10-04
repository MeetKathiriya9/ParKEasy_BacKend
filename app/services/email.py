"""Outbound email delivery (DOC sections 24 and 25 both call for an email
integration).

The DOC names "Nodemailer or transactional email provider" as the intended
technology, but it stops there: it never specifies credentials or a provider.
Rather than block the password-reset flow on infrastructure that does not exist
in this project yet, delivery sits behind a `Transport` interface with two
implementations:

    console  writes the rendered message to the log. The default, so the flow
             is demonstrable end-to-end with no SMTP account.
    smtp     real mail via the standard library's `smtplib`, which covers
             Gmail / SendGrid / SES SMTP endpoints without adding a dependency.

`send()` never raises on a delivery failure: a 502 from the mail provider must
not turn into a 500 for the caller, and it must not be distinguishable from the
neutral "if the account exists we emailed you" response. Failures are logged and
swallowed; the token's own TTL is the real expiry guarantee.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage as MIMEMessage
from email.utils import formataddr
from typing import Protocol

import anyio

from app.core.config import Settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class EmailMessage:
    """A single outbound message."""

    to: str
    subject: str
    text_body: str
    html_body: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    """Anything that can deliver an `EmailMessage`."""

    name: str

    def send(self, message: EmailMessage) -> None:  # pragma: no cover - protocol
        ...


class ConsoleTransport:
    """Log the message instead of sending it. Default outside production."""

    name = "console"

    def send(self, message: EmailMessage) -> None:
        logger.info(
            "email[console] to=%s subject=%s\n%s",
            message.to,
            message.subject,
            message.text_body,
        )


class SmtpTransport:
    """Deliver over SMTP with the standard library."""

    name = "smtp"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _build(self, message: EmailMessage) -> MIMEMessage:
        settings = self._settings
        mime = MIMEMessage()
        mime["Subject"] = message.subject
        mime["From"] = formataddr((settings.email_from_name, settings.email_from))
        mime["To"] = message.to
        for key, value in message.headers.items():
            mime[key] = value
        mime.set_content(message.text_body)
        if message.html_body:
            # Alternative part, so text-only mail clients still see the body.
            mime.add_alternative(message.html_body, subtype="html")
        return mime

    def _connect(self) -> smtplib.SMTP:
        settings = self._settings
        if settings.smtp_use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=15
            )
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
            if settings.smtp_use_tls:
                server.starttls()
        server.login(settings.smtp_username, settings.smtp_password)
        return server

    def send(self, message: EmailMessage) -> None:
        with self._connect() as server:
            server.send_message(self._build(message))
        logger.info("email[smtp] sent to=%s subject=%s", message.to, message.subject)


def build_transport(settings: Settings) -> Transport:
    """Pick a transport from configuration.

    `smtp` is honoured only when the credentials are actually present, so a
    half-finished environment silently falls back to console rather than
    raising on the first reset request.
    """
    if settings.email_transport == "smtp":
        if settings.smtp_configured:
            return SmtpTransport(settings)
        logger.warning(
            "EMAIL_TRANSPORT=smtp but SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD are "
            "incomplete; falling back to the console transport."
        )
    return ConsoleTransport()


async def send(message: EmailMessage, settings: Settings) -> bool:
    """Deliver `message`. Returns True when handed to a real transport.

    Delivery is blocking I/O, so it runs in a worker thread to keep the event
    loop free. Errors are logged and swallowed; see the module docstring.
    """

    transport = build_transport(settings)
    try:
        await anyio.to_thread.run_sync(transport.send, message)
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("email[%s] delivery to %s failed: %s", transport.name, message.to, exc)
        return False
    return transport.name != "console"
