"""
Saved web routines: a named list of browser steps Friday can replay on
demand (``friday web routine run <name>``) or on a schedule (an automation).

Routines live as YAML in ``<runtime home>/data/routines/<name>.yaml``::

    description: Check my electricity bill
    steps:
      - goto: https://example-power.com/login
      - fill: {target: Email, value: "${env:POWER_EMAIL}"}
      - fill: {target: Password, value: "${env:POWER_PASSWORD}"}
      - click: Sign in
      - wait: Amount due
      - extract: {target: "css=.amount-due", as: amount}
      - screenshot: bill.png

Targets are visible text, labels, placeholders, or selectors (``css=...``).
Secrets are referenced as ``${env:NAME}`` and never stored in the file.
Steps that would submit, buy, send or delete still ask first.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from friday.config import ROOT_DIR, config
from friday.web.browser import Browser
from friday.web.safety import ConfirmationRequired

_cfg = config.get("web", {}) or {}
ROUTINES_DIR = ROOT_DIR / str(_cfg.get("routines_dir", "data/routines"))
SCREENSHOT_DIR = ROOT_DIR / str(_cfg.get("screenshot_dir", "data/screenshots"))
STEP_KINDS = ("goto", "click", "fill", "press", "select", "wait", "read", "extract", "screenshot", "scroll", "back")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ENV_RE = re.compile(r"\$\{env:(\w+)\}")

TEMPLATE = """\
# Friday web routine. Run with: friday web routine run {name}
# Targets: visible text, a field's label/placeholder, or a selector like css=.price
# Secrets: write ${{env:NAME}} and set NAME in your environment or .env, never the value itself.
description: {description}
steps:
  - goto: https://example.com
  - read: page
  # - fill: {{target: Email, value: "${{env:MY_EMAIL}}"}}
  # - click: Sign in
  # - wait: Dashboard
  # - extract: {{target: "css=.balance", as: balance}}
  # - screenshot: example.png
"""


class RoutineError(ValueError):
    pass


@dataclass
class Routine:
    name: str
    description: str = ""
    steps: list[dict] = field(default_factory=list)
    path: Path | None = None


@dataclass
class RoutineResult:
    outputs: dict[str, str] = field(default_factory=dict)
    screenshots: list[Path] = field(default_factory=list)
    steps_done: int = 0
    stopped: str | None = None  # why it stopped early, if it did

    @property
    def ok(self) -> bool:
        return self.stopped is None

    def summary(self, limit: int = 600) -> str:
        parts = [f"{k}: {v}" for k, v in self.outputs.items()]
        parts += [f"screenshot: {p}" for p in self.screenshots]
        if self.stopped:
            parts.append(f"stopped: {self.stopped}")
        text = "\n".join(parts) or f"{self.steps_done} step(s) done"
        return text if len(text) <= limit else text[: limit - 1] + "…"


def check_name(name: str) -> str:
    name = name.strip().lower().replace(" ", "-")
    if not _NAME_RE.match(name):
        raise RoutineError("Routine names use letters, digits, - and _ (e.g. check-bill).")
    return name


def normalize_step(step) -> dict:
    """Validate one step; returns {'kind': ..., ...fields}."""
    if not isinstance(step, dict) or not step:
        raise RoutineError(f"each step must be a mapping like {{click: Sign in}}, got {step!r}")
    kind = next(iter(step))
    if kind not in STEP_KINDS:
        raise RoutineError(f"unknown step {kind!r}; use one of {', '.join(STEP_KINDS)}")
    value = step[kind]
    if kind in ("fill", "select"):
        if not isinstance(value, dict) or "target" not in value or "value" not in value:
            raise RoutineError(f"{kind} needs {{target: ..., value: ...}}")
        return {"kind": kind, "target": str(value["target"]), "value": str(value["value"])}
    if kind == "press":
        if isinstance(value, dict):
            return {"kind": kind, "target": value.get("target"), "key": str(value.get("key", "Enter"))}
        return {"kind": kind, "target": None, "key": str(value)}
    if kind == "extract":
        if not isinstance(value, dict) or "target" not in value:
            raise RoutineError("extract needs {target: ..., as: name}")
        return {"kind": kind, "target": str(value["target"]), "as": str(value.get("as") or value["target"])}
    if kind in ("read", "back"):
        return {"kind": kind, "as": value if isinstance(value, str) else "page"}
    if value in (None, ""):
        raise RoutineError(f"{kind} needs a value")
    return {"kind": kind, "value": value}


def parse_routine(name: str, data) -> Routine:
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list) or not data["steps"]:
        raise RoutineError(f"routine {name!r} needs a non-empty 'steps' list")
    for step in data["steps"]:
        normalize_step(step)
    return Routine(name=name, description=str(data.get("description") or ""), steps=data["steps"])


def routine_path(name: str, directory: Path | None = None) -> Path:
    return (directory or ROUTINES_DIR) / f"{check_name(name)}.yaml"


def load_routine(name: str, directory: Path | None = None) -> Routine:
    path = routine_path(name, directory)
    if not path.exists():
        raise RoutineError(f"No routine named {name!r}. See: friday web routine list")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RoutineError(f"{path} is not valid YAML: {exc}") from exc
    routine = parse_routine(check_name(name), data)
    routine.path = path
    return routine


def list_routines(directory: Path | None = None) -> list[Routine]:
    directory = directory or ROUTINES_DIR
    out = []
    for path in sorted(directory.glob("*.yaml")) if directory.exists() else []:
        try:
            out.append(load_routine(path.stem, directory))
        except RoutineError:
            out.append(Routine(path.stem, "(invalid - fix the file)", [], path))
    return out


def save_routine(routine: Routine, directory: Path | None = None, overwrite: bool = False) -> Path:
    path = routine_path(routine.name, directory)
    if path.exists() and not overwrite:
        raise RoutineError(f"A routine named {routine.name!r} already exists.")
    parse_routine(routine.name, {"steps": routine.steps})
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"description": routine.description, "steps": routine.steps}
    path.write_text(yaml.safe_dump(body, sort_keys=False, allow_unicode=True), encoding="utf-8")
    routine.path = path
    return path


def new_template(name: str, description: str = "", directory: Path | None = None) -> Path:
    path = routine_path(name, directory)
    if path.exists():
        raise RoutineError(f"A routine named {name!r} already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE.format(name=check_name(name), description=description or name), encoding="utf-8")
    return path


def resolve_value(value: str) -> str:
    def repl(match):
        var = match.group(1)
        if var not in os.environ:
            raise RoutineError(f"environment variable {var} is not set (used as ${{env:{var}}})")
        return os.environ[var]

    return _ENV_RE.sub(repl, str(value))


async def run_steps(browser: Browser, steps: list, log=None, screenshot_dir: Path | None = None) -> RoutineResult:
    """Run steps in an open browser. Stops (without raising) at the first failure or refusal."""
    result = RoutineResult()
    say = log or (lambda message: None)
    for index, raw in enumerate(steps, start=1):
        step = normalize_step(raw)
        kind = step["kind"]
        try:
            if kind == "goto":
                say(f"{index}. open {step['value']}")
                await browser.goto(resolve_value(step["value"]))
            elif kind == "click":
                say(f"{index}. click {step['value']}")
                await browser.click(str(step["value"]))
            elif kind == "fill":
                say(f"{index}. fill {step['target']}")
                await browser.fill(step["target"], resolve_value(step["value"]))
            elif kind == "select":
                say(f"{index}. select {step['value']} in {step['target']}")
                await browser.select(step["target"], resolve_value(step["value"]))
            elif kind == "press":
                say(f"{index}. press {step['key']}")
                await browser.press(step["target"], step["key"])
            elif kind == "wait":
                say(f"{index}. wait for {step['value']}")
                await browser.wait(step["value"])
            elif kind == "scroll":
                await browser.scroll(str(step["value"]))
            elif kind == "back":
                await browser.back()
            elif kind == "read":
                page = await browser.read()
                result.outputs[step["as"]] = page.text
            elif kind == "extract":
                result.outputs[step["as"]] = await browser.text_of(step["target"])
                say(f"{index}. {step['as']} = {result.outputs[step['as']][:80]}")
            elif kind == "screenshot":
                target = Path(str(step["value"]))
                if not target.is_absolute():
                    target = (screenshot_dir or SCREENSHOT_DIR) / f"{datetime.now():%Y%m%d-%H%M%S}-{target.name}"
                result.screenshots.append(await browser.screenshot(target))
        except ConfirmationRequired as exc:
            result.stopped = f"needs your OK to {exc}"
            return result
        except Exception as exc:
            result.stopped = f"step {index} ({kind}) failed: {str(exc).splitlines()[0] if str(exc) else exc!r}"
            return result
        result.steps_done = index
    return result


async def run_routine(routine: Routine, confirm=None, headless: bool | None = None, log=None) -> RoutineResult:
    from friday.web.safety import deny

    async with Browser(headless=headless, profile=True, confirm=confirm or deny) as browser:
        return await run_steps(browser, routine.steps, log=log)
