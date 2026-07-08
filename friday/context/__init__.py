"""Friday Hub — Project context awareness package.

Scans the working directory to detect project type, git status, file
structure, and frameworks. Provides enriched context for agent prompts.
"""

from friday.context.context_manager import ContextManager, get_context

__all__ = ["ContextManager", "get_context"]
