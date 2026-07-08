"""Claude Code Adapter — wraps the `claude` CLI."""

from friday.adapters.base import BaseAdapter


class ClaudeCodeAdapter(BaseAdapter):
    def __init__(self, cfg: dict):
        super().__init__("claude-code", cfg)
