"""SMTP sender tests with a fake SMTP class: no network."""

import smtplib
from email.message import EmailMessage
from typing import Any

import pytest

from app.config import Settings
from app.integrations.email import EmailSendError, SmtpSender


class FakeSMTP:
    """Records what the sender does instead of talking to a server."""

    instances: list["FakeSMTP"] = []
    fail_login = False

    def __init__(self, host: str, port: int, timeout: float, context: Any = None) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = ["ssl"] if context is not None else []
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *args: object) -> None:
        self.calls.append("quit")

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        self.calls.append(f"login:{user}")

    def send_message(self, message: EmailMessage) -> None:
        self.calls.append("send")
        self.sent.append(message)


@pytest.fixture(autouse=True)
def reset_fake() -> None:
    FakeSMTP.instances = []
    FakeSMTP.fail_login = False


def make_sender(**kwargs: Any) -> SmtpSender:
    return SmtpSender(
        "sandbox.smtp.mailtrap.io",
        2525,
        "mette@fjordflow.dk",
        user="inbox-user",
        password="secret",
        smtp_class=FakeSMTP,  # type: ignore[arg-type]
        smtp_ssl_class=FakeSMTP,  # type: ignore[arg-type]
        **kwargs,
    )


def test_send_uses_starttls_login_and_builds_headers() -> None:
    message_id = make_sender().send("anna@leman.com", "Hello", "Hi Anna,\n\nBody.")

    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("sandbox.smtp.mailtrap.io", 2525)
    assert smtp.calls == ["starttls", "login:inbox-user", "send", "quit"]
    message = smtp.sent[0]
    assert message["To"] == "anna@leman.com"
    assert message["From"] == "mette@fjordflow.dk"
    assert message["Subject"] == "Hello"
    assert message.get_content().startswith("Hi Anna,")
    assert message_id == message["Message-ID"]
    assert message_id.endswith("@fjordflow.dk>")


def test_ssl_mode_skips_starttls() -> None:
    make_sender(use_ssl=True).send("anna@leman.com", "Hello", "Body")

    assert FakeSMTP.instances[0].calls == ["ssl", "login:inbox-user", "send", "quit"]


def test_redirect_sends_to_test_address_and_keeps_intended_recipient_in_subject() -> None:
    make_sender(redirect_to="me@example.com").send("anna@leman.com", "Hello", "Body")

    message = FakeSMTP.instances[0].sent[0]
    assert message["To"] == "me@example.com"
    assert message["Subject"] == "[TEST, intended for anna@leman.com] Hello"


def test_auth_error_has_a_helpful_message() -> None:
    FakeSMTP.fail_login = True

    with pytest.raises(EmailSendError, match="App Password"):
        make_sender().send("anna@leman.com", "Hello", "Body")


def test_from_settings_requires_host_and_from() -> None:
    with pytest.raises(EmailSendError, match="SMTP is not configured"):
        SmtpSender.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]
