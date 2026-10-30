from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.integrations.email import EmailSendError
from app.models.icp import ICP, load_icp
from app.tools import ToolContext
from app.tools.email import EmailContent

ROOT = Path(__file__).resolve().parents[1]

SAMPLE_HTML = """
<html>
  <head>
    <title>Nordic Freight A/S</title>
    <meta name="description" content="Logistics for the Nordics">
    <script>var tracking = 1;</script>
    <style>body { color: red; }</style>
  </head>
  <body>
    <nav>
      <a href="/">Home</a>
      <a href="/services">Services</a>
      <a href="/en/about-us/">About us</a>
      <a href="https://www.nordicfreight.dk/karriere">Karriere</a>
      <a href="/services/">Services again</a>
      <a href="#top">Top</a>
      <a href="mailto:info@nordicfreight.dk">Mail</a>
      <a href="/brochure.pdf">Brochure</a>
      <a href="https://linkedin.com/company/nordic-freight">LinkedIn</a>
    </nav>
    <h1>Nordic Freight</h1>
    <p>We move goods across Denmark and Sweden.   We are hiring an accountant.</p>
  </body>
</html>
"""


@pytest.fixture
def settings() -> Settings:
    # _env_file=None: tests must not depend on a developer's local .env.
    return Settings(_env_file=None, agent_max_steps=10, fetch_max_chars=8000)  # type: ignore[call-arg]


@pytest.fixture
def icp() -> ICP:
    return load_icp(ROOT / "config" / "icp.yaml")


def make_http(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def html_http() -> httpx.Client:
    return make_http(
        lambda request: httpx.Response(
            200, html=SAMPLE_HTML, headers={"content-type": "text/html; charset=utf-8"}
        )
    )


def usage(input_tokens: int, output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


class FakeEmailWriter:
    """Stands in for anthropic.Anthropic: beta.messages.parse returns a canned email."""

    def __init__(self, stop_reason: str = "end_turn") -> None:
        self.stop_reason = stop_reason
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        content = EmailContent(
            subject="161 new trailers at Nordic Freight",
            body="Hi Nordic Freight team,\n\nCongrats on adding 161 new trailers.",
        )
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            parsed_output=None if self.stop_reason == "refusal" else content,
            usage=usage(200, 80),
        )


@pytest.fixture
def email_writer() -> FakeEmailWriter:
    return FakeEmailWriter()


@pytest.fixture
def ctx(
    settings: Settings, icp: ICP, html_http: httpx.Client, email_writer: FakeEmailWriter
) -> ToolContext:
    return ToolContext(settings=settings, icp=icp, http=html_http, llm=email_writer)  # type: ignore[arg-type]


class FakeSender:
    """Stands in for SmtpSender: records emails instead of sending them."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> str:
        if self.fail:
            raise EmailSendError("SMTP login failed.")
        self.sent.append((to, subject, body))
        return "<msg-1@fjordflow.dk>"
