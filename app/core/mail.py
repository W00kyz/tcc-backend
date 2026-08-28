"""E-mail delivery, wrapped behind a protocol so tests inject a fake instead of touching the
network (docs/CLAUDE.base.md — Tests). Production wiring points SmtpMailer at Mailpit."""

from email.message import EmailMessage
from typing import Protocol

import aiosmtplib


class Mailer(Protocol):
    async def send(self, *, to: str, subject: str, body: str) -> None: ...


class SmtpMailer:
    def __init__(self, *, host: str, port: int, from_address: str) -> None:
        self._host = host
        self._port = port
        self._from_address = from_address

    async def send(self, *, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self._from_address
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        await aiosmtplib.send(message, hostname=self._host, port=self._port)


class RecordingMailer:
    """Test double: records every call instead of sending anything."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})
