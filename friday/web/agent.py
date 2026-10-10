"""
A goal-driven browsing agent: "go to X and find/do Y".

Each turn the LLM sees the goal, what it has done so far, and the current
page (URL, title, a text excerpt and a numbered list of clickable/typeable
elements), and answers with one JSON action. Friday performs it in the real
browser and loops until the model says ``done`` (with an answer), gives up,
or the step limit is reached.

Committing actions (buy, send, post, submit, delete...) are checked by
:mod:`friday.web.safety` and need the user's OK. Every successful action is
recorded as a routine step, so a task done once can be saved and replayed
without the LLM (``friday web do ... --save name``).
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from friday.web.browser import Browser, Element
from friday.web.safety import ConfirmationRequired

log = logging.getLogger(__name__)

LlmFn = Callable[[str], Awaitable[str]]
ACTIONS = ("goto", "click", "fill", "press", "select", "scroll", "back", "done", "fail")

_PROMPT = """You are Friday, operating a real web browser for your user. Reach the goal in as few steps as possible.

GOAL: {goal}

Steps so far:
{history}

Current page: {url}
Title: {title}
Page text (excerpt):
{text}

Interactive elements:
{elements}

Reply with ONLY one JSON object, one of:
{{"action": "goto", "value": "<url>"}}
{{"action": "click", "target": "@<id>"}}
{{"action": "fill", "target": "@<id>", "value": "<text to type>"}}
{{"action": "press", "target": "@<id>", "value": "Enter"}}
{{"action": "select", "target": "@<id>", "value": "<option label>"}}
{{"action": "scroll", "value": "down"}}
{{"action": "back"}}
{{"action": "done", "answer": "<the result for the user, with the facts/links they asked for>"}}
{{"action": "fail", "answer": "<why it can't be done>"}}
Add "thought": "<one short sentence>" to explain. Never make up information that isn't on the pages.
If the user declined a step, find another way or finish with what you have.
"""


@dataclass
class AgentResult:
    goal: str
    answer: str
    success: bool
    steps: list[dict] = field(default_factory=list)  # replayable routine steps
    transcript: list[str] = field(default_factory=list)
    final_url: str = ""


async def _default_llm(prompt: str) -> str:
    from friday.models import router as model_router

    response = await model_router.chat(messages=[{"role": "user", "content": prompt}], role="brain",
                                       json_mode=True, max_tokens=800)
    return response["content"]


def _target_label(target: str, elements: dict[int, Element]) -> str:
    """Turn '@12' into a replayable target (the element's visible label)."""
    if target.startswith("@") and target[1:].isdigit():
        element = elements.get(int(target[1:]))
        if element and element.label:
            return element.label
    return target


async def run_agent(
    goal: str,
    browser: Browser,
    start_url: str | None = None,
    llm: LlmFn | None = None,
    max_steps: int = 15,
    say: Callable[[str], None] | None = None,
) -> AgentResult:
    from friday.models.parsing import parse_json_object

    llm = llm or _default_llm
    say = say or (lambda message: None)
    result = AgentResult(goal=goal, answer="", success=False)
    if start_url:
        await browser.goto(start_url)
        result.steps.append({"goto": browser.page.url})
        result.transcript.append(f"opened {browser.page.url}")
    else:
        await browser.goto("https://duckduckgo.com/")
        result.transcript.append("opened https://duckduckgo.com/ (search engine)")

    for _ in range(max_steps):
        page = await browser.read()
        elements = await browser.elements()
        by_id = {e.id: e for e in elements}
        prompt = _PROMPT.format(
            goal=goal,
            history="\n".join(f"{i}. {line}" for i, line in enumerate(result.transcript[-12:], 1)) or "(none)",
            url=browser.page.url,
            title=page.title,
            text=page.text[:3500] or "(empty)",
            elements="\n".join(e.describe() for e in elements[:120]) or "(none)",
        )
        try:
            decision = parse_json_object(await llm(prompt))
        except Exception as exc:
            result.answer = f"I couldn't get a decision from the AI model: {exc}"
            break
        action = str(decision.get("action", "")).lower()
        target = str(decision.get("target") or "")
        value = str(decision.get("value") or "")
        if decision.get("thought"):
            say(str(decision["thought"]))
        if action not in ACTIONS:
            result.transcript.append(f"invalid action {json.dumps(decision)[:120]}")
            continue
        if action in ("done", "fail"):
            result.answer = str(decision.get("answer") or "").strip()
            result.success = action == "done"
            break
        label = _target_label(target, by_id) if target else ""
        step = None
        try:
            if action == "goto":
                say(f"open {value}")
                await browser.goto(value)
                step = {"goto": browser.page.url}
            elif action == "click":
                say(f"click {label}")
                await browser.click(target)
                step = {"click": label}
            elif action == "fill":
                say(f"type into {label}")
                await browser.fill(target, value)
                step = {"fill": {"target": label, "value": value}}
            elif action == "press":
                key = value or "Enter"
                say(f"press {key}")
                await browser.press(target or None, key)
                step = {"press": {"target": label, "key": key} if target else key}
            elif action == "select":
                await browser.select(target, value)
                step = {"select": {"target": label, "value": value}}
            elif action == "scroll":
                await browser.scroll(value or "down")
                step = {"scroll": value or "down"}
            elif action == "back":
                await browser.back()
                step = {"back": True}
            result.transcript.append(f"{action} {label or value}".strip() + " -> ok")
            if step:
                result.steps.append(step)
        except ConfirmationRequired as exc:
            say(f"skipped: you declined to {exc}")
            result.transcript.append(f"{action} {label} -> USER DECLINED ({exc}). Do not retry it.")
        except Exception as exc:
            first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            result.transcript.append(f"{action} {label or value} -> error: {first[:160]}")
    else:
        result.answer = result.answer or f"I stopped after {max_steps} steps without finishing."
    result.final_url = browser.page.url
    if not result.answer:
        result.answer = "I couldn't finish this."
    return result
