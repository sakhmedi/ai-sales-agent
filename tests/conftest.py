from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.models.icp import ICP, load_icp
from app.tools import ToolContext

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


@pytest.fixture
def ctx(settings: Settings, icp: ICP, html_http: httpx.Client) -> ToolContext:
    return ToolContext(settings=settings, icp=icp, http=html_http)
