"""
Version Guard — Update resilience engine.

Tracks agent versions in a lock file. When a version changes, runs a smoke
test (the adapter's health check). If it passes, the new version is accepted.
If it fails, the agent is marked incompatible and Hermes stops routing to it
until a later smoke test passes again.
"""

import asyncio
import json
import logging
from datetime import datetime

from friday.config import config, ROOT_DIR

log = logging.getLogger(__name__)

_health_cfg = config.get("health", {})
_LOCK_PATH = ROOT_DIR / _health_cfg.get("version_lock_path", "data/versions.lock.json")

INCOMPATIBLE = "incompatible"


class VersionGuard:
    def __init__(self, lock_path=None):
        self.lock_path = lock_path or _LOCK_PATH
        self.lock_data = self._load()

    def _load(self) -> dict:
        if self.lock_path.exists():
            try:
                data = json.loads(self.lock_path.read_text("utf-8"))
                if isinstance(data, dict) and isinstance(data.get("agents"), dict):
                    return data
            except Exception as exc:
                log.warning("Ignoring unreadable version lock %s: %s", self.lock_path, exc)
        return {"agents": {}, "last_checked": None}

    def _save(self):
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.lock_path.with_suffix(self.lock_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.lock_data, indent=2, default=str), "utf-8")
            tmp.replace(self.lock_path)
        except OSError as exc:
            log.warning("Could not write version lock %s: %s", self.lock_path, exc)

    def check_version(self, agent_id: str, current_version: str) -> dict:
        """Check if version changed since last known."""
        stored = self.lock_data["agents"].get(agent_id)
        if not stored:
            return {"changed": False, "old": None, "new": current_version, "first_seen": True}
        if stored["version"] != current_version:
            return {"changed": True, "old": stored["version"], "new": current_version, "first_seen": False}
        return {"changed": False, "old": stored["version"], "new": current_version, "first_seen": False}

    def is_incompatible(self, agent_id: str) -> bool:
        """True when the last smoke test for this agent's version failed."""
        stored = self.lock_data["agents"].get(agent_id) or {}
        return stored.get("status") == INCOMPATIBLE

    async def guard(self, adapter) -> dict:
        """Full version guard check for one adapter (detects as a side effect)."""
        det = await adapter.detect()
        if not det["installed"]:
            return {"status": "unavailable", "agent": adapter.id, "error": det["error"]}

        vc = self.check_version(adapter.id, det["version"])

        if vc["first_seen"]:
            self._record(adapter.id, det["version"], "ok")
            return {"status": "new", "agent": adapter.id, "version": det["version"]}

        previously_incompatible = self.is_incompatible(adapter.id)
        if vc["changed"] or previously_incompatible:
            # Version changed (or still failing) — run the smoke test.
            health = await adapter.health_check(refresh=False)
            if health["healthy"]:
                self._record(adapter.id, det["version"], "ok")
                if not vc["changed"]:
                    return {"status": "recovered", "agent": adapter.id, "version": det["version"]}
                return {
                    "status": "updated", "agent": adapter.id,
                    "old_version": vc["old"], "new_version": vc["new"],
                }
            self._record(adapter.id, det["version"], INCOMPATIBLE)
            return {
                "status": INCOMPATIBLE, "agent": adapter.id,
                "old_version": vc["old"], "new_version": vc["new"],
                "error": health.get("message"),
            }

        return {"status": "ok", "agent": adapter.id, "version": det["version"]}

    async def guard_all(self, adapters: dict) -> dict:
        """Guard every adapter concurrently and persist the lock file once."""
        ids = list(adapters)
        outcomes = await asyncio.gather(
            *(self.guard(adapters[aid]) for aid in ids), return_exceptions=True
        )
        results = {}
        for aid, outcome in zip(ids, outcomes):
            if isinstance(outcome, BaseException):
                results[aid] = {"status": "error", "agent": aid, "error": str(outcome)}
            else:
                results[aid] = outcome
        self.lock_data["last_checked"] = datetime.now().isoformat()
        self._save()
        return results

    def _record(self, agent_id: str, version: str, status: str):
        self.lock_data["agents"][agent_id] = {
            "version": version, "status": status,
            "updated": datetime.now().isoformat(),
        }

    def get_data(self) -> dict:
        return self.lock_data
