"""Codex CLI Adapter — wraps the `codex` CLI."""

from friday.adapters.base import BaseAdapter


class CodexAdapter(BaseAdapter):
    def __init__(self, cfg: dict):
        super().__init__("codex", cfg)
