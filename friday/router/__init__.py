"""Friday Hub — Router package.

LLM-based intelligent task routing. Uses a local model to analyze each
task and select the best available agent based on capabilities, past
performance, and task characteristics.

Functions:
    route           Route a task to the best available agent
    get_fallbacks   Get the fallback chain for a given agent
"""

from friday.router.ai_router import route, get_fallbacks

__all__ = ["route", "get_fallbacks"]
