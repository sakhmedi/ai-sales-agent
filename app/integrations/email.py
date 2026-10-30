"""SMTP email sending.

Never called by the agent: emails are sent only from app/outbox.py, after a human approves them.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Self

from app.config import Settings

logger = logging.getLogger(__name__)


class EmailSendError(Exception):
    """Sending failed. The message is safe to show and store (no credentials)."""


class SmtpSender:
    def __init__(
        self,
        host: str,
        port: int,
        from_address: str,
        *,
        user: str = "",
        password: str = "",
        use_ssl: bool = False,
        timeout: float = 10.0,
        redirect_to: str = "",
        smtp_class: type[smtplib.SMTP] = smtplib.SMTP,
        smtp_ssl_class: type[smtplib.SMTP_SSL] = smtplib.SMTP_SSL,
    ) -> None:
        self._host = host
        self._port = port
        self._from = from_address
        self._user = user
        self._password = password
        self._use_ssl = use_ssl
        self._timeout = timeout
        self._redirect_to = redirect_to
        self._smtp_class = smtp_class
        self._smtp_ssl_class = smtp_ssl_class

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        if not settings.smtp_host or not settings.smtp_from:
            raise EmailSendError("SMTP is not configured: set SMTP_HOST and SMTP_FROM in .env.")
        return cls(
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_from,
            user=settings.smtp_user,
            password=settings.smtp_password.get_secret_value(),
            use_ssl=settings.smtp_use_ssl,
            timeout=settings.http_timeout_seconds,
            redirect_to=settings.email_redirect_to,
        )

    def build_message(self, to: str, subject: str, body: str) -> EmailMessage:
        if self._redirect_to:
            subject = f"[TEST, intended for {to}] {subject}"
            to = self._redirect_to

        message = EmailMessage()
        message["From"] = self._from
        message["To"] = to
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=self._from.rpartition("@")[2] or None)
        message.set_content(body)
        return message

    def send(self, to: str, subject: str, body: str) -> str:
        """Send a plain-text email and return its Message-ID."""
        message = self.build_message(to, subject, body)
        context = ssl.create_default_context()
        try:
            if self._use_ssl:
                with self._smtp_ssl_class(
                    self._host, self._port, timeout=self._timeout, context=context
                ) as smtp:
                    self._login_and_send(smtp, message)
            else:
                with self._smtp_class(self._host, self._port, timeout=self._timeout) as smtp:
                    smtp.starttls(context=context)
                    self._login_and_send(smtp, message)
        except smtplib.SMTPAuthenticationError as e:
            raise EmailSendError(
                "SMTP login failed. For Gmail use an App Password, for Mailtrap the inbox's "
                "SMTP credentials."
            ) from e
        except (smtplib.SMTPException, OSError) as e:
            raise EmailSendError(f"Could not send email via {self._host}: {e}") from e

        logger.info("Email sent to %s (%s)", message["To"], message["Message-ID"])
        return str(message["Message-ID"])

    def _login_and_send(self, smtp: smtplib.SMTP, message: EmailMessage) -> None:
        if self._user:
            smtp.login(self._user, self._password)
        smtp.send_message(message)
