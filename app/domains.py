"""Company domains: one normalization used for HubSpot lookups, lead dedup and fetch limits."""

from urllib.parse import urlparse


def company_domain(website: str) -> str:
    """'https://www.Example.dk/about' -> 'example.dk'."""
    url = website if "://" in website else f"https://{website}"
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


def is_same_site(url: str, domain: str) -> bool:
    """True if `url` is on `domain` or one of its subdomains.

    'careers.leman.com' matches 'leman.com'; 'leman.com.evil.io' and 'evilleman.com' do not.
    """
    host = (urlparse(url).hostname or "").lower()
    return host == domain or host.endswith(f".{domain}")
