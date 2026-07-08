"""Friday Hub — Health monitoring package.

Detects installed agents, runs health checks, and tracks version changes
to maintain system resilience.

Classes:
    HealthMonitor   Coordinates detection and health checks for all agents
    VersionGuard    Tracks CLI versions and runs smoke tests on changes
"""

from friday.health.monitor import HealthMonitor
from friday.health.version_guard import VersionGuard

__all__ = ["HealthMonitor", "VersionGuard"]
