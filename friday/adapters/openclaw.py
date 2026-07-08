"""OpenClaw Adapter — wraps the `openclaw` CLI."""

from friday.adapters.base import BaseAdapter


class OpenClawAdapter(BaseAdapter):
    def __init__(self, cfg: dict):
        super().__init__("openclaw", cfg)
