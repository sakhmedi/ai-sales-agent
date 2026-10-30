import logging
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

from app.tools.base import Tool, ToolContext, ToolError

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; AISalesAgent/0.1)"
NOISE_TAGS = ["script", "style", "noscript", "svg", "iframe", "form"]


class FetchWebsiteInput(BaseModel):
    url: str


class WebsiteContent(BaseModel):
    url: str
    title: str
    meta_description: str
    text: str
    truncated: bool


def _normalize_url(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ToolError(f"Invalid URL: {url!r}. Only http(s) URLs are allowed.")
    return url


def extract_text(html: str) -> tuple[str, str, str]:
    """Return (title, meta description, visible text) from an HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(NOISE_TAGS):
        tag.decompose()

    title = soup.title.get_text(strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"})
    meta_description = str(meta.get("content", "")) if meta else ""
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return title, meta_description, text


def fetch_company_website(params: FetchWebsiteInput, ctx: ToolContext) -> WebsiteContent:
    url = _normalize_url(params.url)
    try:
        response = ctx.http.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=ctx.settings.http_timeout_seconds,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as e:
        raise ToolError(f"Could not fetch {url}: {e}") from e

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type:
        raise ToolError(f"{url} returned non-HTML content ({content_type or 'unknown'}).")

    title, meta_description, text = extract_text(response.text)
    max_chars = ctx.settings.fetch_max_chars
    truncated = len(text) > max_chars
    logger.info("Fetched %s: %d chars (truncated=%s)", response.url, len(text), truncated)

    return WebsiteContent(
        url=str(response.url),
        title=title,
        meta_description=meta_description,
        text=text[:max_chars],
        truncated=truncated,
    )


TOOL = Tool(
    name="fetch_company_website",
    description=(
        "Download a web page and return its title, meta description and visible text "
        "(scripts and styles removed, length-limited). Use it on the company homepage first; "
        "fetch an 'about', 'careers' or 'customers' page if you need more evidence."
    ),
    input_model=FetchWebsiteInput,
    handler=fetch_company_website,
)
