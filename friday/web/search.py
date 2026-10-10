"""
Web search for research and ``friday web search``.

Providers (``web.search_provider`` in friday.yaml):

* ``duckduckgo`` (default) - no key needed. Uses DuckDuckGo's HTML endpoint;
  if that is blocked it retries the same page in the browser.
* ``brave`` - Brave Search API, needs ``BRAVE_API_KEY``.
* ``searxng`` - your own SearXNG instance at ``web.searxng_url``.
"""

import logging
import os
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

from friday.config import config
from friday.web.browser import USER_AGENT

log = logging.getLogger(__name__)

_cfg = config.get("web", {}) or {}
PROVIDER = str(_cfg.get("search_provider") or "duckduckgo").lower()
SEARXNG_URL = str(_cfg.get("searxng_url") or "").rstrip("/")
DDG_URL = "https://html.duckduckgo.com/html/"


class SearchError(RuntimeError):
    pass


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


_OPEN_TAG_RE = re.compile(r"<(a|div)\b([^>]*)>", re.I)
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*("[^"]*"|\'[^\']*\')')
_TAG_RE = re.compile(r"<[^>]+>")


def _strip(html: str) -> str:
    return " ".join(unescape(_TAG_RE.sub("", html)).split())


def _ddg_target(href: str) -> str:
    href = unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target
    return href


def parse_duckduckgo(html: str, limit: int = 10) -> list[SearchResult]:
    """Results from a DuckDuckGo HTML results page (ads skipped)."""
    results: list[SearchResult] = []
    seen: set[str] = set()
    current: SearchResult | None = None
    html = html or ""
    for match in _OPEN_TAG_RE.finditer(html):
        tag, attr_text = match.group(1).lower(), match.group(2)
        attrs = {k.lower(): v[1:-1] for k, v in _ATTR_RE.findall(attr_text)}
        classes = attrs.get("class", "").split()
        if tag == "div" and "result__snippet" not in classes:
            continue
        close = html.find(f"</{tag}", match.end())
        inner = html[match.end(): close if close != -1 else len(html)]
        if "result__a" in classes:
            current = None
            href = attrs.get("href", "")
            if "ad_domain=" in href or "/y.js" in href:
                continue
            url = _ddg_target(href)
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or parsed.netloc.endswith("duckduckgo.com") or url in seen:
                continue
            if len(results) >= limit:
                break
            seen.add(url)
            current = SearchResult(_strip(inner), url)
            results.append(current)
        elif "result__snippet" in classes and current is not None and not current.snippet:
            current.snippet = _strip(inner)
    return results


async def _duckduckgo(query: str, limit: int) -> list[SearchResult]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"}
    html = ""
    errors = []
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
            response = await client.post(DDG_URL, data={"q": query, "kl": "wt-wt"})
            response.raise_for_status()
            html = response.text
    except httpx.HTTPError as exc:
        log.info("DuckDuckGo over HTTP failed: %s", exc)
        errors.append(str(exc) or type(exc).__name__)
    results = parse_duckduckgo(html, limit)
    if results:
        return results
    # Blocked or a bot check: a real browser usually gets through.
    try:
        from friday.web.browser import Browser

        async with Browser() as browser:
            await browser.goto(f"{DDG_URL}?q={quote_plus(query)}")
            results = parse_duckduckgo(await browser.page.content(), limit)
    except Exception as exc:
        log.info("DuckDuckGo in the browser failed: %s", exc)
        errors.append(str(exc).split("Call log:")[0].strip())
    if not results and html and "anomaly" in html.lower():
        raise SearchError("DuckDuckGo is rate-limiting this computer. Try again in a few minutes, "
                          "or set web.search_provider to brave (BRAVE_API_KEY) or searxng.")
    if not results and errors and not html:
        raise SearchError(f"DuckDuckGo couldn't be reached ({errors[0][:160]}). Check the internet connection, "
                          "or set web.search_provider to brave or searxng.")
    return results


async def _brave(query: str, limit: int) -> list[SearchResult]:
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        raise SearchError("web.search_provider is brave but BRAVE_API_KEY is not set")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(limit, 20)},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        )
        response.raise_for_status()
    items = (response.json().get("web") or {}).get("results") or []
    return [SearchResult(_strip(i.get("title", "")), i.get("url", ""), _strip(i.get("description", "")))
            for i in items if i.get("url")][:limit]


async def _searxng(query: str, limit: int) -> list[SearchResult]:
    if not SEARXNG_URL:
        raise SearchError("web.search_provider is searxng but web.searxng_url is empty")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f"{SEARXNG_URL}/search", params={"q": query, "format": "json"})
        response.raise_for_status()
    items = response.json().get("results") or []
    return [SearchResult(i.get("title", ""), i.get("url", ""), i.get("content", "")) for i in items
            if i.get("url")][:limit]


async def search(query: str, limit: int = 8, provider: str | None = None) -> list[SearchResult]:
    """Search the web. Raises :class:`SearchError` when the provider can't answer."""
    provider = (provider or PROVIDER).lower()
    handler = {"duckduckgo": _duckduckgo, "ddg": _duckduckgo, "brave": _brave, "searxng": _searxng}.get(provider)
    if handler is None:
        raise SearchError(f"unknown search provider {provider!r} (use duckduckgo, brave or searxng)")
    try:
        return await handler(query, limit)
    except httpx.HTTPError as exc:
        raise SearchError(f"{provider} search failed: {exc}") from exc
