"""
Readable-text extraction from HTML with only the standard library.

Used for pages fetched over plain HTTP (fast path for research) and for
anything the browser hands back as raw HTML. Drops scripts, styles,
navigation, headers, footers, forms and hidden elements; keeps headings,
paragraphs and list items as separate lines.
"""

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

_SKIP = {"script", "style", "noscript", "template", "svg", "nav", "footer", "header", "aside", "form",
         "button", "select", "option", "iframe", "canvas"}
_BLOCK = {"p", "div", "section", "article", "main", "li", "ul", "ol", "br", "tr", "table", "h1", "h2", "h3",
          "h4", "h5", "h6", "blockquote", "pre", "dd", "dt", "figcaption", "td", "th"}
_VOID = {"br", "img", "hr", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed", "param",
         "track"}


@dataclass
class Page:
    url: str
    title: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (text, absolute url)


class _Extractor(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.skip_depth = 0
        self.stack: list[str] = []
        self.parts: list[str] = []
        self.title = ""
        self.in_title = False
        self.links: list[tuple[str, str]] = []
        self.link_href: str | None = None
        self.link_text: list[str] = []
        self.main_start: int | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = "hidden" in attrs or attrs.get("aria-hidden") == "true" or \
            re.search(r"display\s*:\s*none", attrs.get("style") or "", re.I)
        if tag in _VOID:
            if tag == "br" and not self.skip_depth:
                self.parts.append("\n")
            return
        self.stack.append(tag)
        if self.skip_depth or tag in _SKIP or hidden:
            self.skip_depth += 1
            return
        if tag == "title":
            self.in_title = True
        if tag in ("main", "article") and self.main_start is None:
            self.main_start = len(self.parts)
        if tag in _BLOCK:
            self.parts.append("\n")
        if tag.startswith("h") and tag[1:].isdigit():
            self.parts.append("#" * int(tag[1]) + " ")
        if tag == "li":
            self.parts.append("- ")
        if tag == "a" and attrs.get("href"):
            self.link_href = attrs["href"]
            self.link_text = []

    def handle_endtag(self, tag):
        if tag in _VOID or tag not in self.stack:
            return
        while self.stack:
            open_tag = self.stack.pop()
            if self.skip_depth:
                self.skip_depth -= 1
            elif open_tag == "title":
                self.in_title = False
            elif open_tag == "a" and self.link_href is not None:
                text = " ".join("".join(self.link_text).split())
                href = urljoin(self.base_url, self.link_href)
                if text and urlparse(href).scheme in ("http", "https"):
                    self.links.append((text, href))
                self.link_href = None
            if open_tag in _BLOCK:
                self.parts.append("\n")
            if open_tag == tag:
                break

    def handle_data(self, data):
        if self.in_title and not self.skip_depth:
            self.title += data
            return
        if self.skip_depth or "title" in self.stack:
            return
        self.parts.append(data)
        if self.link_href is not None:
            self.link_text.append(data)


def clean_text(text: str) -> str:
    """Collapse whitespace and blank lines; drop very short noise lines."""
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if line and line not in ("-", "#"):
            lines.append(line)
    out: list[str] = []
    for line in lines:
        if out and out[-1] == line:
            continue  # repeated boilerplate
        out.append(line)
    return "\n".join(out)


def extract(html: str, url: str = "") -> Page:
    """Parse HTML into a :class:`Page` with title, readable text and links."""
    parser = _Extractor(url)
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:  # malformed markup: keep whatever was parsed
        pass
    parts = parser.parts
    # Prefer <main>/<article> when it holds most of the content.
    if parser.main_start is not None:
        main = clean_text("".join(parts[parser.main_start:]))
        if len(main) > 200:
            parts = [main]
    return Page(url=url, title=" ".join(unescape(parser.title).split()), text=clean_text("".join(parts)),
                links=parser.links)


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
_WORD_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "a an and are as at be by for from has have how i in is it its of on or that the this to was what when where "
    "which who why will with you your do does did can could should would me my we our best top vs about into "
    "than then there their they them these those also more most".split()
)


def keywords(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in STOPWORDS and len(w) > 1}


def sentences(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        for sentence in _SENTENCE_RE.split(line.lstrip("- ")):
            sentence = sentence.strip()
            if 40 <= len(sentence) <= 400:
                out.append(sentence)
    return out


def relevant_passages(text: str, query: str, limit: int = 5) -> list[str]:
    """The sentences that best match the query's keywords, in page order."""
    terms = keywords(query)
    if not terms:
        return sentences(text)[:limit]
    scored = []
    for index, sentence in enumerate(sentences(text)):
        words = keywords(sentence)
        hits = len(terms & words)
        if hits:
            scored.append((hits / len(terms) + min(len(words), 30) / 300, index, sentence))
    best = sorted(scored, reverse=True)[:limit]
    return [s for _, _, s in sorted(best, key=lambda item: item[1])]
