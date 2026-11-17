"""Tests for the web layer (friday.web): extraction, safety, search parsing,
research, routines, intents, and - when a browser is available - real
Playwright runs against a local test site."""

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from typer.testing import CliRunner

from friday.assistant.intent import parse_rules
from friday.web import research as research_mod
from friday.web.extract import extract, relevant_passages
from friday.web.research import Report, Source, _clean_citations, research, save_report
from friday.web.routines import (
    Routine,
    RoutineError,
    list_routines,
    load_routine,
    new_template,
    normalize_step,
    resolve_value,
    save_routine,
)
from friday.web.safety import is_sensitive_label, needs_confirmation
from friday.web.search import SearchResult, parse_duckduckgo

ARTICLE = """<!doctype html><html><head><title>Solar panels &amp; you</title><style>p{color:red}</style>
<script>var tracking = "do not include";</script></head><body>
<nav><a href="/home">Home</a> <a href="/about">About</a></nav>
<header>Site header noise</header>
<main><h1>How efficient are solar panels?</h1>
<p>Modern residential solar panels convert between 18 and 23 percent of sunlight into electricity.</p>
<p>Panel efficiency drops slightly in hot weather, by roughly 0.3 percent per degree above 25C.</p>
<p>Unrelated: the company was founded in a garage by two friends who liked surfing a lot.</p>
<p hidden>Hidden text must not appear.</p>
<p>Read the <a href="/guide">installation guide</a> or <a href="https://example.org/x">a partner</a>.</p>
</main><footer>Copyright footer noise</footer></body></html>"""


# ── extraction ───────────────────────────────────────────────────────


class TestExtract:
    def test_readable_text(self):
        page = extract(ARTICLE, "https://site.test/solar")
        assert page.title == "Solar panels & you"
        assert "# How efficient are solar panels?" in page.text
        assert "18 and 23 percent" in page.text
        for noise in ("do not include", "Site header noise", "Copyright footer", "Hidden text", "color:red"):
            assert noise not in page.text

    def test_links_are_absolute(self):
        links = dict(extract(ARTICLE, "https://site.test/solar").links)
        assert links["installation guide"] == "https://site.test/guide"
        assert links["a partner"] == "https://example.org/x"

    def test_malformed_html_does_not_raise(self):
        page = extract("<div><p>Unclosed <b>tags everywhere<p>more text here", "")
        assert "Unclosed" in page.text

    def test_relevant_passages_prefers_matching_sentences(self):
        text = extract(ARTICLE).text
        passages = relevant_passages(text, "solar panel efficiency hot weather", limit=2)
        assert any("hot weather" in p for p in passages)
        assert not any("garage" in p for p in passages)


# ── safety ───────────────────────────────────────────────────────────


class TestSafety:
    @pytest.mark.parametrize("label", ["Place order", "Buy now", "Send", "Post", "Pay ₹499", "Delete account",
                                       "Confirm booking", "Submit application", "Subscribe"])
    def test_committing_labels(self, label):
        assert is_sensitive_label(label)
        assert needs_confirmation("click", label)

    @pytest.mark.parametrize("label", ["Search", "Next page", "Read more", "Pricing", "Add to cart", "Accept all"])
    def test_harmless_labels(self, label):
        assert not needs_confirmation("click", label)

    def test_search_form_submit_is_fine(self):
        assert needs_confirmation("press", form={"is_search": True, "submit_labels": ["Go"]}) is None
        assert needs_confirmation("press", form={"submit_labels": ["Search"]}) is None

    def test_other_form_submit_needs_ok(self):
        assert needs_confirmation("press", form={"submit_labels": ["Save"]})
        assert needs_confirmation("submit", form={"has_payment": True, "submit_labels": ["Continue"]})

    def test_payment_fields(self):
        assert needs_confirmation("fill", "card-number cc-number Card number")
        assert needs_confirmation("fill", "email Email address") is None


# ── search parsing ───────────────────────────────────────────────────


DDG_HTML = """
<div class="result results_links_deep result--ad"><h2 class="result__title">
<a rel="nofollow" class="result__a" href="https://duckduckgo.com/y.js?ad_domain=shop.test&amp;u3=x">Ad: buy stuff</a></h2></div>
<div class="result results_links results_links_deep web-result"><div class="links_main links_deep result__body">
<h2 class="result__title"><a rel="nofollow" class="result__a"
 href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fplaywright.dev%2Fpython%2Fdocs%2Fintro%3Fa%3D1%26b%3D2&amp;rut=abc">Installation | <b>Playwright</b> Python</a></h2>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">Playwright was created <b>specifically</b> for end-to-end testing.</a>
</div></div>
<div class="result"><h2 class="result__title"><a class="result__a" href="https://pypi.org/project/playwright/">playwright &middot; PyPI</a></h2>
<div class="result__snippet">Python version of the Playwright testing library.</div></div>
"""


class TestSearchParsing:
    def test_parse_duckduckgo(self):
        results = parse_duckduckgo(DDG_HTML)
        assert [r.url for r in results] == ["https://playwright.dev/python/docs/intro?a=1&b=2",
                                            "https://pypi.org/project/playwright/"]
        assert results[0].title == "Installation | Playwright Python"
        assert results[0].snippet == "Playwright was created specifically for end-to-end testing."
        assert results[1].title == "playwright · PyPI"
        assert results[1].snippet.startswith("Python version")

    def test_limit_and_empty(self):
        assert len(parse_duckduckgo(DDG_HTML, limit=1)) == 1
        assert parse_duckduckgo("") == []


# ── research pipeline (no network) ───────────────────────────────────


def _fake_web(pages: dict[str, str]):
    async def search_fn(query, n):
        return [SearchResult(f"Title {i}", url, f"snippet {i}") for i, url in enumerate(pages)]

    async def fetch_fn(urls):
        return {u: extract(pages[u], u) for u in urls if pages.get(u)}

    return search_fn, fetch_fn


PAGES = {
    "https://a.test/solar": ARTICLE,
    "https://b.test/heat": "<html><title>Heat</title><body><article>" + "<p>Solar panel efficiency falls as "
    "temperature rises, which matters in hot climates like India.</p>" * 3 + "<p>" + "filler words " * 40 +
    "</p></article></body></html>",
    "https://c.test/broken": "",
}


class TestResearch:
    def test_llm_report_with_citations(self):
        search_fn, fetch_fn = _fake_web(PAGES)
        prompts = []

        async def llm(prompt):
            prompts.append(prompt)
            if "search queries" in prompt:
                return '{"queries": ["solar efficiency heat"]}'
            return "<think>hmm</think>Panels convert 18-23% of sunlight [1]. Heat lowers output [2][7]."

        report = asyncio.run(research("how efficient are solar panels", search_fn=search_fn, fetch_fn=fetch_fn,
                                      llm=llm))
        assert report.used_llm
        assert [s.url for s in report.sources] == ["https://a.test/solar", "https://b.test/heat"]
        assert report.answer == "Panels convert 18-23% of sunlight [1]. Heat lowers output [2]."
        assert "[1] Solar panels & you (https://a.test/solar)" in prompts[-1]

    def test_extractive_when_no_llm(self):
        search_fn, fetch_fn = _fake_web(PAGES)
        report = asyncio.run(research("solar panel efficiency", use_llm=False, search_fn=search_fn,
                                      fetch_fn=fetch_fn))
        assert not report.used_llm
        assert "No AI model" in report.answer
        assert "[1]" in report.answer and "[2]" in report.answer

    def test_llm_failure_falls_back(self):
        search_fn, fetch_fn = _fake_web(PAGES)

        async def llm(prompt):
            raise RuntimeError("No LLM providers available")

        report = asyncio.run(research("solar panel efficiency", search_fn=search_fn, fetch_fn=fetch_fn, llm=llm))
        assert not report.used_llm
        assert any("AI summary unavailable" in note for note in report.notes)

    def test_snippets_when_nothing_readable(self):
        search_fn, _ = _fake_web(PAGES)

        async def fetch_fn(urls):
            return {}

        report = asyncio.run(research("solar", use_llm=False, search_fn=search_fn, fetch_fn=fetch_fn))
        assert report.sources and "snippets only" in report.notes[0]

    def test_no_results_raises(self):
        async def search_fn(q, n):
            return []

        with pytest.raises(research_mod.ResearchError):
            asyncio.run(research("anything", use_llm=False, search_fn=search_fn))

    def test_one_domain_does_not_dominate(self):
        results = [SearchResult("x", f"https://same.test/{i}") for i in range(5)] + [SearchResult("y", "https://o.test")]
        picked = research_mod._pick_candidates(results, 10)
        assert [r.url for r in picked] == ["https://same.test/0", "https://same.test/1", "https://o.test"]

    def test_save_report(self, tmp_path):
        report = Report("Best monitors?", "Answer [1]", [Source(1, "Mon", "https://m.test", "", [])], True)
        path = save_report(report, tmp_path / "r.md")
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# Best monitors?") and "1. [Mon](https://m.test)" in text

    def test_clean_citations(self):
        assert _clean_citations("a [1] b [3] c [0].", 2) == "a [1] b c."


# ── routines ─────────────────────────────────────────────────────────


class TestRoutines:
    def test_normalize_steps(self):
        assert normalize_step({"click": "Sign in"}) == {"kind": "click", "value": "Sign in"}
        assert normalize_step({"press": "Enter"}) == {"kind": "press", "target": None, "key": "Enter"}
        assert normalize_step({"read": True})["as"] == "page"
        with pytest.raises(RoutineError):
            normalize_step({"hack": "x"})
        with pytest.raises(RoutineError):
            normalize_step({"fill": "no target"})

    def test_save_load_list(self, tmp_path):
        save_routine(Routine("check-bill", "Bill", [{"goto": "https://x.test"}, {"read": "page"}]), tmp_path)
        loaded = load_routine("check-bill", tmp_path)
        assert loaded.description == "Bill" and len(loaded.steps) == 2
        assert [r.name for r in list_routines(tmp_path)] == ["check-bill"]
        with pytest.raises(RoutineError):
            save_routine(Routine("check-bill", "", [{"goto": "x"}]), tmp_path)

    def test_template_is_valid(self, tmp_path):
        new_template("morning-news", "News", tmp_path)
        assert load_routine("morning-news", tmp_path).steps[0] == {"goto": "https://example.com"}

    def test_bad_names(self, tmp_path):
        with pytest.raises(RoutineError):
            load_routine("../etc/passwd", tmp_path)

    def test_env_values(self, monkeypatch):
        monkeypatch.setenv("FRIDAY_TEST_SECRET", "s3cret")
        assert resolve_value("pw=${env:FRIDAY_TEST_SECRET}") == "pw=s3cret"
        with pytest.raises(RoutineError):
            resolve_value("${env:FRIDAY_SURELY_UNSET_VAR}")


# ── intents ──────────────────────────────────────────────────────────


class TestWebIntents:
    @pytest.mark.parametrize("text,kind,title", [
        ("research best budget 4k monitors 2026", "research", "best budget 4k monitors 2026"),
        ("do some research on electric scooters in Pune", "research", "electric scooters in Pune"),
        ("compare iphone 17 and pixel 10", "research", "compare iphone 17 and pixel 10"),
        ("search the web for python 3.14 release date", "web_search", "python 3.14 release date"),
        ("google weather pune", "web_search", "weather pune"),
        ("open example.com", "web_open", "example.com"),
        ("go to amazon.in and find the price of a kindle", "web_task", "find the price of a kindle"),
        ("take a screenshot of https://news.ycombinator.com", "screenshot", "https://news.ycombinator.com"),
        ("run my check-bill routine", "routine", "check-bill"),
    ])
    def test_rules(self, text, kind, title):
        intent = parse_rules(text)
        assert (intent.kind, intent.title) == (kind, title)

    def test_task_url(self):
        assert parse_rules("go to amazon.in and find the price of a kindle").extra["url"] == "amazon.in"

    @pytest.mark.parametrize("text", ["look into why the tests fail", "search the repo for TODOs"])
    def test_coding_requests_stay_agent_tasks(self, text):
        assert parse_rules(text) is None

    def test_recurring_research_and_routine(self):
        intent = parse_rules("every weekday at 8am research AI news")
        assert (intent.kind, intent.action, intent.title) == ("automate", "research", "AI news")
        intent = parse_rules("every day at 9am run my check-bill routine")
        assert (intent.action, intent.title) == ("web_routine", "check-bill")


# ── CLI (no browser needed) ──────────────────────────────────────────


class TestWebCli:
    def test_routine_commands(self, tmp_path, monkeypatch):
        from friday.cli import app
        from friday.web import routines

        monkeypatch.setattr(routines, "ROUTINES_DIR", tmp_path)
        runner = CliRunner()
        assert runner.invoke(app, ["web", "routine", "new", "daily-news"]).exit_code == 0
        listed = runner.invoke(app, ["web", "routine", "list"])
        assert listed.exit_code == 0 and "daily-news" in listed.output
        assert runner.invoke(app, ["web", "routine", "rm", "daily-news"]).exit_code == 0
        assert runner.invoke(app, ["web", "routine", "rm", "daily-news"]).exit_code == 1

    def test_research_command(self, tmp_path, monkeypatch):
        from friday.cli import app

        search_fn, fetch_fn = _fake_web(PAGES)
        monkeypatch.setattr(research_mod, "search", lambda q, limit: search_fn(q, limit))
        monkeypatch.setattr(research_mod, "fetch_pages", lambda urls, use_browser=None: fetch_fn(urls))
        out = tmp_path / "solar.md"
        result = CliRunner().invoke(app, ["research", "solar panel efficiency", "--no-llm", "--out", str(out)])
        assert result.exit_code == 0, result.output
        assert "https://a.test/solar" in out.read_text(encoding="utf-8")

    def test_automate_research(self, tmp_path, monkeypatch):
        from friday.assistant import cli as assistant_cli
        from friday.assistant.service import Assistant
        from friday.assistant.store import AssistantStore
        from friday.cli import app

        store = AssistantStore(tmp_path / "a.db")
        monkeypatch.setattr(assistant_cli, "_assistant", lambda: Assistant(store))
        result = CliRunner().invoke(app, ["automate", "add", "every day at 8am", "--research", "AI news"])
        assert result.exit_code == 0, result.output
        assert store.list_automations()[0].action == "research"
        bad = CliRunner().invoke(app, ["automate", "add", "every day at 8am", "--routine", "nope"])
        assert bad.exit_code == 2
        store.close()


# ── real browser against a local site ────────────────────────────────


SITE = {
    "/": ARTICLE,
    "/shop": """<html><head><title>Shop</title></head><body><h1>Kindle Paperwhite</h1>
<p class="price">Rs 13,999</p>
<form role="search" action="/search"><input type="search" name="q" placeholder="Search products">
<button>Search</button></form>
<form method="post" action="/order"><label>Quantity <input name="qty" value="1"></label>
<button type="submit">Place order</button></form></body></html>""",
    "/search": "<html><head><title>Results</title></head><body><h1>Results for {q}</h1>"
               "<a href='/shop'>Kindle Paperwhite - Rs 13,999</a></body></html>",
}


class _Handler(BaseHTTPRequestHandler):
    orders = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        body = SITE.get(url.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        q = parse_qs(url.query).get("q", [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.replace("{q}", q).encode())

    def do_POST(self):
        _Handler.orders.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body>Order placed</body></html>")


@pytest.fixture(scope="module")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _browser_or_skip():
    from friday.web.browser import Browser, BrowserUnavailable

    async def probe():
        async with Browser():
            return True

    try:
        asyncio.run(probe())
    except BrowserUnavailable as exc:
        if os.environ.get("FRIDAY_REQUIRE_BROWSER"):
            raise
        pytest.skip(f"no browser: {exc}")


@pytest.fixture(scope="module")
def browser_ok():
    _browser_or_skip()


@pytest.mark.usefixtures("browser_ok")
class TestRealBrowser:
    def test_open_read_and_screenshot(self, site, tmp_path):
        from friday.web.browser import Browser

        async def go():
            async with Browser() as b:
                await b.goto(site + "/")
                page = await b.read()
                shot = await b.screenshot(tmp_path / "s.png")
                return page, shot

        page, shot = asyncio.run(go())
        assert page.title == "Solar panels & you"
        assert "18 and 23 percent" in page.text
        assert shot.stat().st_size > 1000

    def test_search_form_and_guarded_order(self, site):
        from friday.web.browser import Browser
        from friday.web.safety import ConfirmationRequired

        asked = []

        def confirm(question):
            asked.append(question)
            return False

        async def go():
            async with Browser(confirm=confirm) as b:
                await b.goto(site + "/shop")
                await b.fill("Search products", "kindle")
                await b.press("Search products", "Enter")  # search forms don't need an OK
                await b.page.wait_for_url("**/search?**")
                heading = await b.text_of("css=h1")
                await b.goto(site + "/shop")
                with pytest.raises(ConfirmationRequired):
                    await b.click("Place order")
                return heading

        assert asyncio.run(go()) == "Results for kindle"
        assert len(asked) == 1 and "Place order" in asked[0]
        assert _Handler.orders == []  # nothing was ordered

    def test_routine_run(self, site, tmp_path):
        from friday.web.browser import Browser
        from friday.web.routines import run_steps

        steps = [{"goto": site + "/shop"}, {"extract": {"target": "css=.price", "as": "price"}},
                 {"screenshot": str(tmp_path / "shop.png")}, {"click": "Place order"}]

        async def go():
            async with Browser() as b:
                return await run_steps(b, steps)

        result = asyncio.run(go())
        assert result.outputs["price"] == "Rs 13,999"
        assert result.screenshots and result.screenshots[0].exists()
        assert result.stopped and "needs your OK" in result.stopped
        assert result.steps_done == 3 and _Handler.orders == []

    def test_agent_loop(self, site):
        from friday.web.agent import run_agent
        from friday.web.browser import Browser

        calls = []

        async def llm(prompt):
            calls.append(prompt)
            if len(calls) == 3:
                assert "Results for kindle" in prompt
                return json.dumps({"action": "done", "answer": "The Kindle Paperwhite costs Rs 13,999."})
            box = next(line.split()[0] for line in prompt.splitlines() if line.startswith("@") and "Search products" in line)
            if len(calls) == 1:
                return json.dumps({"action": "fill", "target": box, "value": "kindle", "thought": "search"})
            return json.dumps({"action": "press", "target": box, "value": "Enter"})

        async def go():
            async with Browser() as b:
                return await run_agent("find the kindle price", b, start_url=site + "/shop", llm=llm, max_steps=5)

        result = asyncio.run(go())
        assert result.success and "13,999" in result.answer
        assert result.steps[1] == {"fill": {"target": "Search products", "value": "kindle"}}
        assert result.steps[2] == {"press": {"target": "Search products", "key": "Enter"}}

    def test_research_reads_pages_over_http(self, site):
        from friday.web.research import fetch_pages

        pages = asyncio.run(fetch_pages([site + "/", site + "/missing"], use_browser=False))
        assert list(pages) == [site + "/"]
        assert "18 and 23 percent" in pages[site + "/"].text
