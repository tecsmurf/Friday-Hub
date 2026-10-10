"""
Health Monitor — Detects and health-checks all agents.
"""

import asyncio

from friday.config import config
from friday.adapters.claude_code import ClaudeCodeAdapter
from friday.adapters.codex import CodexAdapter
from friday.adapters.openclaw import OpenClawAdapter
from friday.health.version_guard import VersionGuard

ADAPTER_CLASSES = {
    "claude-code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "openclaw": OpenClawAdapter,
}


class HealthMonitor:
    def __init__(self, agents_cfg: dict | None = None, version_guard: VersionGuard | None = None):
        self.adapters: dict = {}
        self.version_guard = version_guard or VersionGuard()
        self.guard_results: dict = {}
        self._init_adapters(config.get("agents", {}) if agents_cfg is None else agents_cfg)

    def _init_adapters(self, agents_cfg: dict):
        for agent_id, cls in ADAPTER_CLASSES.items():
            cfg = agents_cfg.get(agent_id, {})
            if cfg.get("enabled", True):
                self.adapters[agent_id] = cls(cfg)

    async def guard_all(self) -> dict:
        """Detect every agent and run the version guard (smoke test on change)."""
        self.guard_results = await self.version_guard.guard_all(self.adapters)
        return self.guard_results

    async def check_all(self) -> dict:
        """Run version guard + health check on all agents, concurrently."""
        guard_results = await self.guard_all()

        ids = list(self.adapters)
        # Detection already ran inside the guard; don't spawn --version again.
        healths = await asyncio.gather(
            *(self.adapters[aid].health_check(refresh=False) for aid in ids)
        )
        return {
            aid: {
                "status": self.adapters[aid].get_status(),
                "health": health,
                "guard": guard_results.get(aid, {}),
            }
            for aid, health in zip(ids, healths)
        }

    def get_adapters(self) -> dict:
        return self.adapters

    def is_usable(self, agent_id: str) -> bool:
        """Installed, enabled, and not marked incompatible by the version guard."""
        adapter = self.adapters.get(agent_id)
        if adapter is None:
            return False
        status = adapter.get_status()
        return bool(status.installed and status.enabled) and not self.version_guard.is_incompatible(agent_id)

    async def detect_all(self) -> dict:
        ids = list(self.adapters)
        results = await asyncio.gather(*(self.adapters[aid].detect() for aid in ids))
        return dict(zip(ids, results))
