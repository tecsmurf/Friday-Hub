"""
Helpers for turning LLM responses into structured data.
"""

import json
import re

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def parse_json_object(content: str) -> dict:
    """Extract the first JSON object from an LLM response.

    Tolerates reasoning blocks (``<think>…</think>`` from Qwen3 and similar),
    Markdown code fences, and prose before or after the object.

    Raises:
        ValueError: when no JSON object can be decoded.
    """
    text = _THINK_RE.sub("", content or "").strip()

    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    decoder = json.JSONDecoder()
    for start in (m.start() for m in re.finditer(r"\{", text)):
        try:
            obj, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"No JSON object in model response: {content[:200]!r}")
