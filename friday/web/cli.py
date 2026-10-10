"""
Web CLI commands: ``friday research`` and ``friday web ...``.
"""

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from friday.display import console, show_error, show_info

web_app = typer.Typer(help="Drive a real browser: open, read, search, screenshot, do tasks, run routines.",
                      no_args_is_help=True)
routine_app = typer.Typer(help="Saved web routines Friday can replay.", no_args_is_help=True)
web_app.add_typer(routine_app, name="routine")


def _fail(message: str, code: int = 1):
    show_error(message)
    raise typer.Exit(code)


def _ok(message: str) -> None:
    console.print(Text("  ✓ ", style="bold green") + Text(message))


def _say(message: str) -> None:
    console.print(Text(f"  → {message}", style="dim"))


def _confirm(yes: bool):
    from friday.web.safety import allow, ask_user

    return allow if yes else ask_user


def _run_browser(coro):
    from friday.web.browser import BrowserUnavailable

    try:
        return asyncio.run(coro)
    except BrowserUnavailable as exc:
        _fail(str(exc))
    except KeyboardInterrupt:
        _fail("Interrupted.", code=130)
    except Exception as exc:
        if type(exc).__module__.startswith("playwright"):  # navigation errors, timeouts
            _fail(str(exc).split("Call log:")[0].strip().removeprefix("Error: "))
        raise


# ── research ───────────────────────────────────────────────────────────


def do_research(question: str, sources: Optional[int] = None, out: Optional[Path] = None, no_llm: bool = False,
                browser: Optional[bool] = None, quiet: bool = False):
    from friday.web.research import ResearchError, research, save_report

    try:
        report = _run_browser(research(question, max_sources=sources, use_llm=not no_llm, use_browser=browser,
                                       progress=None if quiet else _say))
    except ResearchError as exc:
        _fail(str(exc))
    path = save_report(report, out)
    if not quiet:
        console.print()
        console.print(Markdown(report.markdown()))
    _ok(f"Saved research report to {path}")
    return report


def research_cmd(
    question: list[str] = typer.Argument(..., help='What to research, e.g. "best budget 4K monitors 2026"'),
    sources: Optional[int] = typer.Option(None, "--sources", "-n", min=1, max=15, help="How many sources to read"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Where to save the Markdown report"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Don't use an AI model; quote the best passages"),
    browser: Optional[bool] = typer.Option(None, "--browser/--no-browser",
                                           help="Always / never read pages in the real browser (default: when needed)"),
):
    """Research a topic on the web and write a cited report."""
    do_research(" ".join(question), sources, out, no_llm, browser)


# ── web ────────────────────────────────────────────────────────────────


def do_open(url: str, show: bool = False, links: bool = False, full: bool = False, screenshot: Optional[Path] = None):
    from friday.web.browser import Browser

    async def _open():
        async with Browser(headless=not show, profile=show) as b:
            await b.goto(url)
            page = await b.read()
            shot = await b.screenshot(screenshot) if screenshot else None
            if show:
                await asyncio.get_running_loop().run_in_executor(None, input, "  Press Enter to close the browser...")
            return page, shot

    page, shot = _run_browser(_open())
    console.print(f"[bold bright_cyan]{escape(page.title or page.url)}[/]  [dim]{escape(page.url)}[/]\n")
    text = page.text if full else page.text[:3000] + ("\n… (use --full for the whole page)" if len(page.text) > 3000 else "")
    console.print(Text(text))
    if links:
        console.print("\n[bold]Links[/]")
        for label, href in page.links[:60]:
            console.print(Text(f"  {label[:60]}  ", style="") + Text(href, style="dim"))
    if shot:
        _ok(f"Screenshot saved to {shot}")


@web_app.command("open")
def web_open(
    url: str = typer.Argument(..., help="Page to open, e.g. example.com"),
    show: bool = typer.Option(False, "--show", help="Open a visible browser window (with your saved sign-ins)"),
    links: bool = typer.Option(False, "--links", help="Also list the links on the page"),
    full: bool = typer.Option(False, "--full", help="Print the whole page text"),
    screenshot: Optional[Path] = typer.Option(None, "--screenshot", help="Also save a screenshot here"),
):
    """Open a page and print its readable text."""
    do_open(url, show, links, full, screenshot)


def do_screenshot(url: str, path: Optional[Path] = None, full: bool = True):
    from datetime import datetime

    from friday.web.browser import Browser
    from friday.web.routines import SCREENSHOT_DIR

    target = path or SCREENSHOT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.png"

    async def _shot():
        async with Browser() as b:
            await b.goto(url)
            return await b.screenshot(target, full_page=full)

    _ok(f"Screenshot saved to {_run_browser(_shot())}")


@web_app.command("shot")
def web_shot(
    url: str = typer.Argument(..., help="Page to capture"),
    path: Optional[Path] = typer.Argument(None, help="Output .png (default: data/screenshots/)"),
    viewport: bool = typer.Option(False, "--viewport", help="Only the visible area, not the full page"),
):
    """Save a screenshot of a page."""
    do_screenshot(url, path, full=not viewport)


def do_search(query: str, count: int = 8):
    from friday.web.search import SearchError, search

    try:
        results = _run_browser(search(query, limit=count))
    except SearchError as exc:
        _fail(str(exc))
    if not results:
        _fail("No results.")
    for i, r in enumerate(results, 1):
        console.print(Text(f"{i}. {r.title}", style="bold") + Text(f"\n   {r.url}", style="cyan"))
        if r.snippet:
            console.print(Text(f"   {r.snippet[:220]}", style="dim"))


@web_app.command("search")
def web_search(
    query: list[str] = typer.Argument(..., help="What to search for"),
    count: int = typer.Option(8, "--count", "-n", min=1, max=25, help="Number of results"),
):
    """Search the web and list results."""
    do_search(" ".join(query), count)


def do_task(goal: str, url: Optional[str] = None, show: bool = False, save: Optional[str] = None,
            max_steps: int = 15, yes: bool = False):
    from friday.web.agent import run_agent
    from friday.web.browser import Browser

    if save:
        from friday.web.routines import RoutineError, check_name

        try:
            save = check_name(save)
        except RoutineError as exc:
            _fail(str(exc), code=2)

    async def _task():
        async with Browser(headless=not show, profile=True, confirm=_confirm(yes)) as b:
            return await run_agent(goal, b, start_url=url, max_steps=max_steps, say=_say)

    result = _run_browser(_task())
    console.print()
    console.print(Markdown(result.answer))
    show_info(f"Finished on {result.final_url}")
    if save and result.success and result.steps:
        from friday.web.routines import Routine, save_routine

        path = save_routine(Routine(save, goal, result.steps), overwrite=True)
        _ok(f"Saved as routine '{save}' ({len(result.steps)} steps): {path}. Replay with: friday web routine run {save}")
    if not result.success:
        raise typer.Exit(1)


@web_app.command("do")
def web_do(
    goal: list[str] = typer.Argument(..., help='The task, e.g. "find the opening hours of the city library"'),
    url: Optional[str] = typer.Option(None, "--url", help="Start on this page"),
    show: bool = typer.Option(False, "--show", help="Watch it work in a visible browser window"),
    save: Optional[str] = typer.Option(None, "--save", help="Save the steps as a routine with this name"),
    max_steps: int = typer.Option(15, "--max-steps", min=1, max=50),
    yes: bool = typer.Option(False, "--yes", help="Don't ask before submitting/sending/buying (careful!)"),
):
    """Let Friday do a task in the browser (needs an AI model)."""
    do_task(" ".join(goal), url, show, save, max_steps, yes)


@web_app.command("login")
def web_login(url: str = typer.Argument(..., help="Site to sign in to, e.g. mail.google.com")):
    """Open a visible browser so you can sign in once; routines and `web do` reuse the session."""
    from friday.web.browser import Browser

    async def _login():
        async with Browser(headless=False, profile=True) as b:
            await b.goto(url)
            await asyncio.get_running_loop().run_in_executor(
                None, input, "  Sign in in the browser window, then press Enter here to save the session...")

    _run_browser(_login())
    _ok("Saved. Friday's browser profile stays signed in to that site.")


@web_app.command("setup")
def web_setup():
    """Download the Chromium build Playwright drives (one time)."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        _fail('Playwright isn\'t installed. Run: pip install "friday-hub[browser]"')
    result = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
    if result.returncode != 0:
        if sys.platform == "win32":
            show_info("Chromium download failed, but Friday can drive Microsoft Edge instead; try: friday web status")
        raise typer.Exit(result.returncode)
    _ok("Browser installed. Try: friday web open example.com")


@web_app.command("status")
def web_status():
    """Check that Friday can start a browser."""
    from friday.web.browser import browser_status

    status = browser_status()
    (_ok if status.startswith("ready") else show_info)(f"Browser: {status}")


# ── routines ───────────────────────────────────────────────────────────


@routine_app.command("list")
def routine_list():
    """List saved routines."""
    from friday.web.routines import list_routines

    routines = list_routines()
    if not routines:
        show_info('No routines yet. Create one with: friday web routine new check-bill  (or friday web do "..." --save name)')
        return
    table = Table(box=box.ROUNDED, border_style="bright_blue", title="[bold bright_cyan]Web routines[/]")
    table.add_column("Name", style="bright_cyan")
    table.add_column("Steps", justify="right")
    table.add_column("Description")
    for r in routines:
        table.add_row(escape(r.name), str(len(r.steps)), escape(r.description))
    console.print(table)


@routine_app.command("new")
def routine_new(
    name: str = typer.Argument(..., help="Routine name, e.g. check-bill"),
    description: str = typer.Option("", "--description", "-d"),
):
    """Create a routine file from a template to edit."""
    from friday.web.routines import RoutineError, new_template

    try:
        path = new_template(name, description)
    except RoutineError as exc:
        _fail(str(exc), code=2)
    _ok(f"Created {path}. Edit its steps, then run: friday web routine run {name}")


@routine_app.command("show")
def routine_show(name: str):
    """Print a routine's steps."""
    from friday.web.routines import RoutineError, load_routine

    try:
        routine = load_routine(name)
    except RoutineError as exc:
        _fail(str(exc))
    console.print(Text(routine.path.read_text(encoding="utf-8")))


def do_routine(name: str, show: bool = False, yes: bool = False):
    from friday.web.routines import RoutineError, load_routine, run_routine

    try:
        routine = load_routine(name)
    except RoutineError as exc:
        _fail(str(exc))
    result = _run_browser(run_routine(routine, confirm=_confirm(yes), headless=not show, log=_say))
    for key, value in result.outputs.items():
        console.print(f"[bold]{escape(key)}[/]")
        console.print(Text(value[:2000] + ("…" if len(value) > 2000 else "")))
    for shot in result.screenshots:
        _ok(f"Screenshot: {shot}")
    if result.stopped:
        hint = " Run it in a terminal to approve, or add --yes." if "needs your OK" in result.stopped and not yes else ""
        _fail(f"Routine '{routine.name}' stopped: {result.stopped}.{hint}")
    _ok(f"Routine '{routine.name}' finished ({result.steps_done} steps).")


@routine_app.command("run")
def routine_run(
    name: str,
    show: bool = typer.Option(False, "--show", help="Run in a visible browser window"),
    yes: bool = typer.Option(False, "--yes", help="Don't ask before submitting/sending/buying (careful!)"),
):
    """Run a saved routine now."""
    do_routine(name, show, yes)


@routine_app.command("rm")
def routine_rm(name: str):
    """Delete a routine."""
    from friday.web.routines import RoutineError, routine_path

    try:
        path = routine_path(name)
    except RoutineError as exc:
        _fail(str(exc), code=2)
    if not path.exists():
        _fail(f"No routine named {name!r}.")
    path.unlink()
    _ok(f"Deleted routine '{name}'.")


def register(app: typer.Typer) -> None:
    app.command("research")(research_cmd)
    app.add_typer(web_app, name="web")
