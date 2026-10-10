"""
A Playwright-driven browser Friday controls.

``Browser`` opens Chromium (or, when Playwright's own Chromium isn't
installed, Microsoft Edge on Windows / Google Chrome elsewhere) and offers
page-level verbs: goto, read, screenshot, click, fill, press, select, scroll,
plus an index of the page's interactive elements for the browsing agent.

With ``profile=True`` it uses a persistent profile under
``<runtime home>/data/browser-profile`` so a site you sign in to once
(``friday web login <url>``) stays signed in for routines and the agent.

Every committing action (click/press/submit that could buy, send, post,
delete...) goes through :mod:`friday.web.safety` and the ``confirm``
callback before it happens.
"""

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from friday.config import ROOT_DIR, config
from friday.web.extract import Page, clean_text, extract
from friday.web.safety import Confirm, ConfirmationRequired, deny, is_payment_field, needs_confirmation

log = logging.getLogger(__name__)

_cfg = config.get("web", {}) or {}
HEADLESS = bool(_cfg.get("headless", True))
CHANNEL = str(_cfg.get("browser_channel") or "") or None
TIMEOUT_MS = int(float(_cfg.get("timeout_seconds", 30)) * 1000)
PROFILE_DIR = ROOT_DIR / str(_cfg.get("profile_dir", "data/browser-profile"))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)

INSTALL_HINT = (
    "Browser automation needs Playwright: pip install \"friday-hub[browser]\" then `friday web setup` "
    "(downloads Chromium). On Windows Friday can also use the Microsoft Edge that's already installed."
)


class BrowserUnavailable(RuntimeError):
    """Playwright or a browser to drive is missing."""


@dataclass
class Element:
    id: int
    tag: str
    type: str
    label: str
    href: str = ""
    value: str = ""

    def describe(self) -> str:
        kind = self.tag if self.tag != "input" else f"input[{self.type or 'text'}]"
        href = f" -> {self.href[:80]}" if self.href and self.tag == "a" else ""
        value = f" (contains \u201c{self.value}\u201d)" if self.value else ""
        return f"@{self.id} {kind} \u201c{self.label}\u201d{value}{href}"


_LABEL_JS = """
const isField = el => (el.tagName === 'INPUT' && !['submit', 'button', 'reset', 'image', 'checkbox', 'radio']
  .includes((el.getAttribute('type') || '').toLowerCase())) || el.tagName === 'TEXTAREA';
const labelOf = el => {
  const labelEl = el.labels && el.labels[0];
  const text = isField(el) ? (el.placeholder || el.title || el.name) : (el.innerText || el.value || el.title || el.alt);
  return (el.getAttribute('aria-label') || (labelEl && labelEl.innerText) || text || el.name || '')
    .trim().replace(/\\s+/g, ' ');
};
"""

_ELEMENTS_JS = """
() => {
""" + _LABEL_JS + """
  document.querySelectorAll('[data-friday-id]').forEach(e => e.removeAttribute('data-friday-id'));
  const sel = 'a[href], button, input:not([type=hidden]), textarea, select, summary, [role=button], [role=link], '
    + '[role=tab], [role=menuitem], [role=checkbox], [role=option], [contenteditable=true]';
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width === 0 || r.height === 0 || st.visibility === 'hidden' || st.display === 'none' || el.disabled) continue;
    i += 1;
    el.setAttribute('data-friday-id', String(i));
    const typed = isField(el);
    out.push({id: i, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
              label: labelOf(el).slice(0, 80), href: el.tagName === 'A' ? el.href : '',
              value: typed ? String(el.value || '').slice(0, 60) : ''});
    if (i >= 150) break;
  }
  return out;
}
"""

_TARGET_INFO_JS = """
el => {
""" + _LABEL_JS + """
  const label = labelOf(el).slice(0, 120);
  const describe = x => [x.name, x.id, x.getAttribute('autocomplete'), x.placeholder,
    x.getAttribute('aria-label'), x.labels && x.labels[0] ? x.labels[0].innerText : ''].join(' ');
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || (tag === 'button' ? 'submit' : '')).toLowerCase();
  const isSubmit = (tag === 'button' || tag === 'input') && (type === 'submit' || type === 'image');
  const f = el.form || el.closest('form');
  let form = null;
  if (f) {
    const subs = Array.from(f.querySelectorAll('button, input[type=submit], input[type=image]'))
      .filter(b => ((b.getAttribute('type') || 'submit').toLowerCase()) !== 'button'
                   && ((b.getAttribute('type') || 'submit').toLowerCase()) !== 'reset')
      .map(b => (b.innerText || b.value || b.getAttribute('aria-label') || '').trim());
    const fields = Array.from(f.querySelectorAll('input, select, textarea'));
    form = {submit_labels: subs, has_password: fields.some(x => x.type === 'password'),
            fields: fields.map(describe).join(' | '),
            is_search: f.getAttribute('role') === 'search' || fields.some(x => x.type === 'search')
              || /search|[?&]q=/i.test(f.getAttribute('action') || '')};
  }
  return {label, field: describe(el), tag, type, is_submit: isSubmit, form};
}
"""

_SELECTOR_HINT = re.compile(r"^(css=|xpath=|text=|role=|id=|//)|[#.\[\]>:=]")


class Browser:
    """Async context manager around one Playwright browser context."""

    def __init__(self, headless: bool | None = None, profile: bool = False, channel: str | None = None,
                 timeout_ms: int | None = None, confirm: Confirm = deny, profile_dir: Path | None = None):
        self.headless = HEADLESS if headless is None else headless
        self.profile = profile
        self.channel = channel if channel is not None else CHANNEL
        self.timeout_ms = timeout_ms or TIMEOUT_MS
        self.confirm = confirm
        self.profile_dir = profile_dir or PROFILE_DIR
        self._pw = None
        self._browser = None
        self.context = None
        self.page = None

    # ── lifecycle ──────────────────────────────────────────────────

    async def __aenter__(self) -> "Browser":
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailable(INSTALL_HINT) from exc
        self._pw = await async_playwright().start()
        try:
            await self._launch()
        except Exception:
            await self._pw.stop()
            raise
        self.context.set_default_timeout(self.timeout_ms)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        return self

    async def __aexit__(self, *exc) -> None:
        for closer in (self.context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:  # already closed by the user
                    pass
        if self._pw is not None:
            await self._pw.stop()

    def _channels(self) -> list[str | None]:
        if self.channel:
            return [self.channel]
        return [None, "msedge" if sys.platform == "win32" else "chrome"]

    async def _launch(self) -> None:
        chromium = self._pw.chromium
        errors = []
        for channel in self._channels():
            kwargs = {"headless": self.headless}
            if channel:
                kwargs["channel"] = channel
            try:
                if self.profile:
                    self.profile_dir.mkdir(parents=True, exist_ok=True)
                    self.context = await chromium.launch_persistent_context(
                        str(self.profile_dir), user_agent=USER_AGENT, accept_downloads=True, **kwargs)
                else:
                    self._browser = await chromium.launch(**kwargs)
                    self.context = await self._browser.new_context(user_agent=USER_AGENT, accept_downloads=True)
                return
            except Exception as exc:
                message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                errors.append(f"{channel or 'chromium'}: {message}")
                if self.profile and re.search(r"ProcessSingleton|in use|lock", str(exc), re.I):
                    raise BrowserUnavailable(
                        "Friday's browser profile is in use by another Friday browser window. Close it and retry."
                    ) from exc
        raise BrowserUnavailable(f"Couldn't start a browser ({'; '.join(errors)}). {INSTALL_HINT}")

    # ── reading ────────────────────────────────────────────────────

    async def goto(self, url: str) -> str:
        url = normalize_url(url)
        await self.page.goto(url, wait_until="domcontentloaded")
        try:
            await self.page.wait_for_load_state("networkidle", timeout=min(self.timeout_ms, 8000))
        except Exception:
            pass  # busy pages never go idle; the DOM is loaded, carry on
        return self.page.url

    async def read(self) -> Page:
        """The current page as title + readable text + links."""
        html = await self.page.content()
        page = extract(html, self.page.url)
        page.title = await self.page.title() or page.title
        if len(page.text) < 200:
            try:
                page.text = clean_text(await self.page.inner_text("body"))
            except Exception:
                pass
        return page

    async def screenshot(self, path: Path, full_page: bool = True) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path), full_page=full_page)
        return path

    async def elements(self) -> list[Element]:
        return [Element(**item) for item in await self.page.evaluate(_ELEMENTS_JS)]

    # ── acting ─────────────────────────────────────────────────────

    async def locate(self, target: str):
        """Resolve a target: ``@12`` (from :meth:`elements`), a selector, or visible text/label."""
        target = target.strip()
        page = self.page
        if re.fullmatch(r"@\d+", target):
            loc = page.locator(f'[data-friday-id="{target[1:]}"]')
            if await loc.count():
                return loc.first
            raise LookupError(f"element {target} is gone; the page changed")
        if _SELECTOR_HINT.search(target):
            try:
                loc = page.locator(target)
                if await loc.count():
                    return loc.first
            except Exception:
                pass  # not a valid selector after all; try it as text
        for finder in (
            lambda: page.get_by_role("button", name=target),
            lambda: page.get_by_role("link", name=target),
            lambda: page.get_by_label(target),
            lambda: page.get_by_placeholder(target),
            lambda: page.get_by_role("textbox", name=target),
            lambda: page.get_by_text(target, exact=True),
            lambda: page.get_by_text(target),
        ):
            loc = finder()
            if await loc.count():
                return loc.first
        raise LookupError(f"nothing on the page matches {target!r}")

    async def _guard(self, action: str, locator, extra: str = "") -> dict:
        info = await locator.evaluate(_TARGET_INFO_JS)
        form = info.get("form")
        if form is not None:
            form["has_payment"] = is_payment_field(form.get("fields", ""))
        if action == "fill":
            reason = needs_confirmation("fill", info.get("field", "") + " " + info.get("label", ""))
        elif action == "click" and info.get("is_submit") and form:
            reason = needs_confirmation("click", info.get("label", "")) or needs_confirmation(
                "submit", form={**form, "submit_labels": [info.get("label", "")]})
        elif action == "press":
            reason = needs_confirmation("press", form=form) if extra.lower() == "enter" else None
        else:
            reason = needs_confirmation(action, info.get("label", ""), form=form)
        if reason:
            question = f"{reason} on {self.page.url}"
            if not self.confirm(question):
                raise ConfirmationRequired(question)
        return info

    async def click(self, target: str) -> str:
        loc = await self.locate(target)
        info = await self._guard("click", loc)
        await loc.click()
        await self._settle()
        return info.get("label", "")

    async def fill(self, target: str, value: str) -> None:
        loc = await self.locate(target)
        await self._guard("fill", loc)
        await loc.fill(value)

    async def press(self, target: str | None, key: str) -> None:
        if target:
            loc = await self.locate(target)
            await self._guard("press", loc, key)
            await loc.press(key)
        else:
            await self.page.keyboard.press(key)
        await self._settle()

    async def select(self, target: str, value: str) -> None:
        loc = await self.locate(target)
        await self._guard("select", loc)
        try:
            await loc.select_option(label=value)
        except Exception:
            await loc.select_option(value)

    async def text_of(self, target: str) -> str:
        loc = await self.locate(target)
        return " ".join((await loc.inner_text()).split())

    async def scroll(self, direction: str = "down") -> None:
        await self.page.mouse.wheel(0, -800 if direction == "up" else 800)
        await asyncio.sleep(0.3)

    async def back(self) -> None:
        await self.page.go_back(wait_until="domcontentloaded")

    async def wait(self, what: str | float) -> None:
        if isinstance(what, (int, float)) or re.fullmatch(r"\d+(\.\d+)?", str(what)):
            await asyncio.sleep(min(float(what), 60))
        else:
            await (await self.locate(str(what))).wait_for(state="visible")

    async def _settle(self) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=min(self.timeout_ms, 10000))
        except Exception:
            pass
        await asyncio.sleep(0.2)


def normalize_url(url: str) -> str:
    url = url.strip().strip("<>\"'")
    if re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I) or url.startswith(("about:", "data:", "file:")):
        return url
    if re.match(r"^(localhost|127\.\d+\.\d+\.\d+|\[::1\])(:\d+)?(/|$)", url, re.I):
        return "http://" + url
    return "https://" + url


async def fetch_with_browser(urls: list[str], headless: bool | None = None) -> dict[str, Page]:
    """Open each URL in a shared throwaway browser; returns the readable pages that loaded."""
    pages: dict[str, Page] = {}
    async with Browser(headless=headless) as browser:
        for url in urls:
            try:
                await browser.goto(url)
                pages[url] = await browser.read()
            except Exception as exc:
                log.info("Browser could not load %s: %s", url, exc)
    return pages


def browser_status() -> str:
    """Human-readable answer to 'can Friday drive a browser here?'."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return "not installed. " + INSTALL_HINT

    async def _probe():
        async with Browser(headless=True) as browser:
            return browser._browser.version if browser._browser else "ok"

    try:
        return f"ready (Chromium {asyncio.run(_probe())})"
    except BrowserUnavailable as exc:
        return f"unavailable: {exc}"
