"""
Health Monitor — Detects and health-checks all agents.
"""

from friday.config import config
from friday.adapters.claude_code import ClaudeCodeAdapter
from friday.adapters.codex import CodexAdapter
from friday.adapters.openclaw import OpenClawAdapter
from friday.health.version_guard import VersionGuard


class HealthMonitor:
    def __init__(self):
        self.adapters: dict = {}
        self.version_guard = VersionGuard()
        self._init_adapters()

    def _init_adapters(self):
        agents_cfg = config.get("agents", {})
        adapter_map = {
            "claude-code": ClaudeCodeAdapter,
            "codex": CodexAdapter,
            "openclaw": OpenClawAdapter,
        }
        for agent_id, cls in adapter_map.items():
            cfg = agents_cfg.get(agent_id, {})
            if cfg.get("enabled", True):
                self.adapters[agent_id] = cls(cfg)

    async def check_all(self) -> dict:
        """Run health check + version guard on all agents."""
        guard_results = await self.version_guard.guard_all(self.adapters)

        results = {}
        for aid, adapter in self.adapters.items():
            health = await adapter.health_check()
            results[aid] = {
                "status": adapter.get_status(),
                "health": health,
                "guard": guard_results.get(aid, {}),
            }
        return results

    def get_adapters(self) -> dict:
        return self.adapters

    async def detect_all(self) -> dict:
        results = {}
        for aid, adapter in self.adapters.items():
            results[aid] = await adapter.detect()
        return results
