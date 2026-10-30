import logging
import re
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

from app.tools.base import Tool, ToolContext, ToolError

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; AISalesAgent/0.1)"
NOISE_TAGS = ["script", "style", "noscript", "svg", "iframe", "form"]

MAX_LINKS = 30
MAX_LINK_TEXT_CHARS = 80
SKIPPED_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip", ".mp4", ".webp")
# Pages that usually hold company facts (HQ country, headcount, hiring). Includes
# Scandinavian variants, since the ICP targets the Nordics.
PRIORITY_KEYWORDS = (
    "about",
    "company",
    "who-we-are",
    "contact",
    "location",
    "office",
    "career",
    "job",
    "team",
    "om-os",
    "om-oss",
    "kontakt",
    "karriere",
    "karriar",
)


class FetchWebsiteInput(BaseModel):
    url: str


class PageLink(BaseModel):
    text: str
    url: str


class WebsiteContent(BaseModel):
    url: str
    title: str
    meta_description: str
    text: str
    truncated: bool
    links: list[PageLink]


def _normalize_url(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ToolError(f"Invalid URL: {url!r}. Only http(s) URLs are allowed.")
    return url


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def extract_links(soup: BeautifulSoup, base_url: str) -> list[PageLink]:
    """Internal page links, likely 'about/contact/careers' pages first, at most MAX_LINKS."""
    base_host = _host(base_url)
    # Keys without trailing slash, so "/about" and "/about/" count once. The page itself is skipped.
    seen: set[str] = {urldefrag(base_url).url.rstrip("/")}
    links: list[PageLink] = []

    for a in soup.find_all("a", href=True):
        url = urldefrag(urljoin(base_url, str(a["href"]))).url
        parsed = urlparse(url)
        if (
            parsed.scheme not in ("http", "https")
            or _host(url) != base_host
            or parsed.path.lower().endswith(SKIPPED_EXTENSIONS)
            or url.rstrip("/") in seen
        ):
            continue
        seen.add(url.rstrip("/"))
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:MAX_LINK_TEXT_CHARS]
        links.append(PageLink(text=text or parsed.path, url=url))

    def is_priority(link: PageLink) -> bool:
        haystack = f"{link.text} {urlparse(link.url).path}".lower()
        return any(keyword in haystack for keyword in PRIORITY_KEYWORDS)

    # sorted() is stable: priority links first, page order kept within each group.
    return sorted(links, key=lambda link: not is_priority(link))[:MAX_LINKS]


def parse_page(html: str, base_url: str) -> tuple[str, str, str, list[PageLink]]:
    """Return (title, meta description, visible text, internal links) from an HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(NOISE_TAGS):
        tag.decompose()

    title = soup.title.get_text(strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"})
    meta_description = str(meta.get("content", "")) if meta else ""
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return title, meta_description, text, extract_links(soup, base_url)


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

    final_url = str(response.url)
    title, meta_description, text, links = parse_page(response.text, final_url)
    max_chars = ctx.settings.fetch_max_chars
    truncated = len(text) > max_chars
    logger.info(
        "Fetched %s: %d chars (truncated=%s), %d links",
        final_url,
        len(text),
        truncated,
        len(links),
    )

    return WebsiteContent(
        url=final_url,
        title=title,
        meta_description=meta_description,
        text=text[:max_chars],
        truncated=truncated,
        links=links,
    )


TOOL = Tool(
    name="fetch_company_website",
    description=(
        "Download a web page and return its title, meta description, visible text "
        "(scripts and styles removed, length-limited) and up to 30 internal links, with "
        "about/contact/careers-style pages listed first. Use the links to pick the next page "
        "to fetch instead of guessing URLs."
    ),
    input_model=FetchWebsiteInput,
    handler=fetch_company_website,
)
