"""
Version Guard — Update resilience engine.

Tracks agent versions in a lock file. When a version changes,
runs a smoke test. If it passes, the new version is accepted.
If it fails, the agent is disabled and tasks fall back.
"""

import json
from pathlib import Path
from datetime import datetime
from friday.config import config, ROOT_DIR

_health_cfg = config.get("health", {})
_LOCK_PATH = ROOT_DIR / _health_cfg.get("version_lock_path", "data/versions.lock.json")


class VersionGuard:
    def __init__(self):
        self.lock_data = self._load()

    def _load(self) -> dict:
        if _LOCK_PATH.exists():
            try:
                return json.loads(_LOCK_PATH.read_text("utf-8"))
            except Exception:
                pass
        return {"agents": {}, "last_checked": None}

    def _save(self):
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LOCK_PATH.write_text(json.dumps(self.lock_data, indent=2, default=str), "utf-8")

    def check_version(self, agent_id: str, current_version: str) -> dict:
        """Check if version changed since last known."""
        stored = self.lock_data["agents"].get(agent_id)
        if not stored:
            return {"changed": False, "old": None, "new": current_version, "first_seen": True}
        if stored["version"] != current_version:
            return {"changed": True, "old": stored["version"], "new": current_version, "first_seen": False}
        return {"changed": False, "old": stored["version"], "new": current_version, "first_seen": False}

    async def guard(self, adapter) -> dict:
        """Full version guard check for one adapter."""
        det = await adapter.detect()
        if not det["installed"]:
            return {"status": "unavailable", "agent": adapter.id, "error": det["error"]}

        vc = self.check_version(adapter.id, det["version"])

        if vc["first_seen"]:
            self._record(adapter.id, det["version"], "ok")
            return {"status": "new", "agent": adapter.id, "version": det["version"]}

        if vc["changed"]:
            # Version changed — run smoke test
            health = await adapter.health_check()
            if health["healthy"]:
                self._record(adapter.id, det["version"], "ok")
                return {
                    "status": "updated", "agent": adapter.id,
                    "old_version": vc["old"], "new_version": vc["new"],
                }
            else:
                self._record(adapter.id, det["version"], "incompatible")
                return {
                    "status": "incompatible", "agent": adapter.id,
                    "old_version": vc["old"], "new_version": vc["new"],
                }

        return {"status": "ok", "agent": adapter.id, "version": det["version"]}

    async def guard_all(self, adapters: dict) -> dict:
        results = {}
        for aid, adapter in adapters.items():
            results[aid] = await self.guard(adapter)
        self.lock_data["last_checked"] = datetime.now().isoformat()
        self._save()
        return results

    def _record(self, agent_id: str, version: str, status: str):
        self.lock_data["agents"][agent_id] = {
            "version": version, "status": status,
            "updated": datetime.now().isoformat(),
        }
        self._save()

    def get_data(self) -> dict:
        return self.lock_data
