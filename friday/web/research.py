"""
Research on the user's behalf: search the web, read the best sources and
write a cited Markdown report.

Pipeline: (optional) the LLM suggests extra search queries -> search ->
fetch pages over HTTP, falling back to the real browser for JavaScript-heavy
or blocking sites -> pick the passages that match the question -> the LLM
writes an answer citing sources as [1], [2]... If no model is reachable the
report is extractive (the most relevant sentences, each cited), so research
still works offline from an LLM.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from friday.config import ROOT_DIR, config
from friday.web.browser import USER_AGENT
from friday.web.extract import Page, extract, relevant_passages
from friday.web.search import SearchResult, search

log = logging.getLogger(__name__)

_cfg = (config.get("web", {}) or {}).get("research", {}) or {}
MAX_SOURCES = int(_cfg.get("max_sources", 6))
OUTPUT_DIR = ROOT_DIR / str(_cfg.get("output_dir", "data/research"))
MAX_BYTES = 3_000_000
_SKIP_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|exe|msi|dmg|mp4|mp3|jpg|jpeg|png|gif)(\?|$)", re.I)

SearchFn = Callable[[str, int], Awaitable[list[SearchResult]]]
FetchFn = Callable[[list[str]], Awaitable[dict[str, Page]]]
LlmFn = Callable[[str], Awaitable[str]]


class ResearchError(RuntimeError):
    pass


@dataclass
class Source:
    n: int
    title: str
    url: str
    text: str
    passages: list[str] = field(default_factory=list)


@dataclass
class Report:
    question: str
    answer: str
    sources: list[Source]
    used_llm: bool
    notes: list[str] = field(default_factory=list)
    path: Path | None = None

    def markdown(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        lines = [f"# {self.question}", "", f"_Researched by Friday on {now:%Y-%m-%d %H:%M} from "
                 f"{len(self.sources)} source(s)._", "", self.answer.strip(), "", "## Sources", ""]
        for s in self.sources:
            lines.append(f"{s.n}. [{s.title or s.url}]({s.url})")
        if self.notes:
            lines += ["", "## Notes", ""] + [f"- {note}" for note in self.notes]
        return "\n".join(lines) + "\n"


# ── fetching ─────────────────────────────────────────────────────────


async def _fetch_http(client: httpx.AsyncClient, url: str) -> Page | None:
    try:
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                return None
            ctype = response.headers.get("content-type", "")
            if "html" not in ctype and "text/plain" not in ctype:
                return None
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    break
            text = body.decode(response.encoding or "utf-8", errors="replace")
            if "text/plain" in ctype:
                return Page(url=str(response.url), text=text)
            return extract(text, str(response.url))
    except (httpx.HTTPError, UnicodeError) as exc:
        log.info("HTTP fetch of %s failed: %s", url, exc)
        return None


async def fetch_pages(urls: list[str], use_browser: bool | None = None) -> dict[str, Page]:
    """Readable pages for ``urls``. ``use_browser``: True=always, False=never, None=only when HTTP fails."""
    pages: dict[str, Page] = {}
    if use_browser is not True:
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"}
        limit = asyncio.Semaphore(4)
        async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
            async def one(url):
                async with limit:
                    return url, await _fetch_http(client, url)

            for url, page in await asyncio.gather(*(one(u) for u in urls)):
                if page and len(page.text) >= 300:
                    pages[url] = page
    missing = [u for u in urls if u not in pages]
    if missing and use_browser is not False:
        try:
            from friday.web.browser import fetch_with_browser

            pages.update({u: p for u, p in (await fetch_with_browser(missing)).items() if len(p.text) >= 300})
        except Exception as exc:  # no Playwright / no browser: HTTP results only
            log.info("Browser fallback unavailable: %s", exc)
    return pages


# ── LLM helpers ──────────────────────────────────────────────────────


async def _default_llm(prompt: str) -> str:
    from friday.models import router as model_router

    response = await model_router.chat(messages=[{"role": "user", "content": prompt}], role="brain",
                                       max_tokens=2000)
    return response["content"]


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


async def _more_queries(question: str, llm: LlmFn) -> list[str]:
    from friday.models.parsing import parse_json_object

    prompt = (
        "Suggest 2 different web search queries that would help answer this question thoroughly. "
        'Reply with ONLY JSON: {"queries": ["...", "..."]}\n\nQuestion: ' + question
    )
    try:
        data = parse_json_object(await llm(prompt))
        return [str(q).strip() for q in data.get("queries", []) if str(q).strip()][:2]
    except Exception as exc:
        log.info("Query expansion skipped: %s", exc)
        return []


_SYNTH_PROMPT = """You are Friday, a research assistant. Answer the question using ONLY the numbered sources below.
Cite every factual claim with the source number in square brackets, e.g. [2] or [1][3].
Write Markdown with:
- a direct answer in 2-4 sentences,
- "## Key findings": 4-8 bullet points with citations,
- "## Caveats": where sources disagree, are old, or the answer is uncertain.
Do not invent sources or facts that are not in the sources.

Question: {question}

Sources:
{sources}
"""


def _source_block(sources: list[Source], budget: int = 14000) -> str:
    per = max(800, budget // max(len(sources), 1))
    blocks = []
    for s in sources:
        body = " ".join(s.passages) if s.passages else ""
        if len(body) < per:
            body = (body + "\n" + s.text[: per - len(body)]).strip()
        blocks.append(f"[{s.n}] {s.title} ({s.url})\n{body[:per]}")
    return "\n\n".join(blocks)


def _clean_citations(text: str, count: int) -> str:
    text = _THINK_RE.sub("", text).strip()
    return re.sub(r"[ \t]?\[(\d+)\]", lambda m: m.group(0) if 1 <= int(m.group(1)) <= count else "", text)


def extractive_answer(question: str, sources: list[Source], per_source: int = 2) -> str:
    bullets = []
    for s in sources:
        for passage in s.passages[:per_source]:
            bullets.append(f"- {passage} [{s.n}]")
    if not bullets:
        bullets = [f"- {s.title} [{s.n}]" for s in sources]
    return ("_No AI model was reachable, so these are the passages that best match the question, "
            "quoted from each source._\n\n## Key findings\n\n" + "\n".join(bullets[:12]))


# ── pipeline ─────────────────────────────────────────────────────────


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60].rstrip("-") or "research"


def _pick_candidates(results: list[SearchResult], want: int) -> list[SearchResult]:
    picked, per_domain, seen = [], {}, set()
    for r in results:
        url = r.url.split("#")[0]
        domain = urlparse(url).netloc.lower().removeprefix("www.")
        if url in seen or _SKIP_EXT.search(urlparse(url).path) or per_domain.get(domain, 0) >= 2:
            continue
        seen.add(url)
        per_domain[domain] = per_domain.get(domain, 0) + 1
        picked.append(r)
        if len(picked) >= want:
            break
    return picked


async def research(
    question: str,
    max_sources: int | None = None,
    use_llm: bool = True,
    use_browser: bool | None = None,
    search_fn: SearchFn | None = None,
    fetch_fn: FetchFn | None = None,
    llm: LlmFn | None = None,
    progress: Callable[[str], None] | None = None,
) -> Report:
    """Research ``question`` and return a :class:`Report` (not yet saved)."""
    question = " ".join(question.split())
    if not question:
        raise ResearchError("What should I research?")
    max_sources = max(1, min(max_sources or MAX_SOURCES, 15))
    search_fn = search_fn or (lambda q, n: search(q, limit=n))
    fetch_fn = fetch_fn or (lambda urls: fetch_pages(urls, use_browser=use_browser))
    llm = llm or _default_llm
    say = progress or (lambda message: None)
    notes: list[str] = []

    queries = [question]
    if use_llm:
        queries += [q for q in await _more_queries(question, llm) if q.lower() != question.lower()]
    results: list[SearchResult] = []
    errors = []
    for query in queries:
        say(f"Searching: {query}")
        try:
            results += await search_fn(query, 10)
        except Exception as exc:
            errors.append(str(exc))
    if not results:
        raise ResearchError("No search results" + (f": {'; '.join(errors)}" if errors else "."))

    candidates = _pick_candidates(results, max_sources * 2)
    say(f"Reading {len(candidates)} pages...")
    pages = await fetch_fn([c.url for c in candidates])

    sources: list[Source] = []
    for c in candidates:
        page = pages.get(c.url)
        if not page:
            continue
        n = len(sources) + 1
        passages = relevant_passages(page.text, question, limit=6) or ([c.snippet] if c.snippet else [])
        sources.append(Source(n, page.title or c.title, c.url, page.text, passages))
        if len(sources) >= max_sources:
            break
    if not sources:  # nothing readable: fall back to search snippets
        notes.append("No page could be read in full; findings are from search-result snippets only.")
        sources = [Source(i + 1, c.title, c.url, c.snippet, [c.snippet] if c.snippet else [])
                   for i, c in enumerate(candidates[:max_sources])]

    used_llm = False
    answer = ""
    if use_llm:
        say("Writing the summary...")
        try:
            answer = _clean_citations(
                await llm(_SYNTH_PROMPT.format(question=question, sources=_source_block(sources))), len(sources))
            used_llm = bool(answer)
        except Exception as exc:
            notes.append(f"AI summary unavailable ({str(exc)[:160]}); showing extracted passages instead.")
    if not answer:
        answer = extractive_answer(question, sources)
    return Report(question, answer, sources, used_llm, notes)


def save_report(report: Report, out: Path | None = None, now: datetime | None = None) -> Path:
    now = now or datetime.now()
    path = Path(out) if out else OUTPUT_DIR / f"{now:%Y-%m-%d}-{_slug(report.question)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.markdown(now), encoding="utf-8")
    report.path = path
    return path
