"""
Base adapter for all external agent CLIs.

Each concrete adapter wraps a third-party command behind the same interface so
the router can execute tasks without knowing the CLI details.
"""

import asyncio
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class AdapterStatus:
    id: str
    name: str
    enabled: bool
    installed: bool | None = None
    healthy: bool | None = None
    version: str | None = None
    last_check: datetime | None = None
    error: str | None = None
    priority: int = 99
    capabilities: list[str] = field(default_factory=list)


@dataclass
class ExecResult:
    success: bool
    output: str
    duration: float
    agent: str
    agent_name: str
    version: str | None = None
    error: str | None = None
    fallback_from: str | None = None


class BaseAdapter:
    """Abstract base class for agent CLI adapters."""

    def __init__(self, agent_id: str, cfg: dict):
        self.id = agent_id
        self.name = cfg.get("name", agent_id)
        self.command = cfg.get("command", agent_id)
        self.enabled = cfg.get("enabled", True)
        self.capabilities = cfg.get("capabilities", [])
        self.execute_args = cfg.get("execute_args")
        self.version_args = cfg.get("version_args")
        self.health_args = cfg.get("health_args")

        # Legacy string patterns are still accepted for custom user configs.
        self.execute_pattern = cfg.get("execute_pattern", "")
        self.version_command = cfg.get("version_command", "")
        self.health_command = cfg.get("health_command")
        self.priority = cfg.get("priority", 99)

        self._version: str | None = None
        self._installed: bool | None = None
        self._healthy: bool | None = None
        self._last_check: datetime | None = None
        self._error: str | None = None

    async def detect(self) -> dict:
        """Check whether the CLI is installed and return version info."""
        if not shutil.which(self.command):
            self._installed = False
            self._error = f"'{self.command}' not found on PATH"
            return {"installed": False, "version": None, "error": self._error}

        try:
            if self.version_args is not None:
                output = await self._exec_args(
                    [self.command, *self._format_args(self.version_args)],
                    timeout=10,
                )
            else:
                cmd_str = self.version_command.replace("{cmd}", self.command)
                output = await self._exec_shell(cmd_str, timeout=10)

            version = self._parse_version(output)
            self._version = version
            self._installed = True
            self._error = None
            return {"installed": True, "version": version, "error": None}
        except Exception as exc:
            self._installed = False
            self._error = str(exc)
            return {"installed": False, "version": None, "error": str(exc)}

    async def health_check(self) -> dict:
        """Run an optional health check."""
        if not self.enabled:
            return {"healthy": False, "message": "Disabled in config"}

        detection = await self.detect()
        if not detection["installed"]:
            self._healthy = False
            return {"healthy": False, "message": f"Not installed: {detection['error']}"}

        if self.health_args is not None:
            try:
                await self._exec_args(
                    [self.command, *self._format_args(self.health_args)],
                    timeout=15,
                )
            except Exception:
                pass
        elif self.health_command:
            try:
                cmd_str = self.health_command.replace("{cmd}", self.command)
                await self._exec_shell(cmd_str, timeout=15)
            except Exception:
                pass

        self._healthy = True
        self._last_check = datetime.now()
        return {"healthy": True, "message": f"OK (v{detection['version']})"}

    async def execute(self, prompt: str, cwd: str | None = None, timeout: float = 120) -> ExecResult:
        """Execute a task via the agent CLI."""
        if not self.enabled:
            raise RuntimeError(f"{self.name} is disabled")

        start = asyncio.get_event_loop().time()
        try:
            if self.execute_args is not None:
                output = await self._exec_args(
                    [self.command, *self._format_args(self.execute_args, prompt=prompt)],
                    timeout=timeout,
                    cwd=cwd,
                )
            else:
                safe_prompt = prompt.replace('"', '\\"')
                cmd_str = (
                    self.execute_pattern
                    .replace("{cmd}", self.command)
                    .replace("{prompt}", safe_prompt)
                )
                output = await self._exec_shell(cmd_str, timeout=timeout, cwd=cwd)

            duration = asyncio.get_event_loop().time() - start
            return ExecResult(
                success=True,
                output=output.strip(),
                duration=round(duration, 2),
                agent=self.id,
                agent_name=self.name,
                version=self._version,
            )
        except Exception as exc:
            duration = asyncio.get_event_loop().time() - start
            return ExecResult(
                success=False,
                output="",
                duration=round(duration, 2),
                agent=self.id,
                agent_name=self.name,
                version=self._version,
                error=str(exc),
            )

    def get_status(self) -> AdapterStatus:
        return AdapterStatus(
            id=self.id,
            name=self.name,
            enabled=self.enabled,
            installed=self._installed,
            healthy=self._healthy,
            version=self._version,
            last_check=self._last_check,
            error=self._error,
            priority=self.priority,
            capabilities=self.capabilities,
        )

    def _format_args(self, args: list, prompt: str | None = None) -> list[str]:
        replacements = {
            "{cmd}": self.command,
            "{prompt}": prompt or "",
        }
        formatted = []
        for arg in args:
            value = str(arg)
            for key, replacement in replacements.items():
                value = value.replace(key, replacement)
            formatted.append(value)
        return formatted

    async def _exec_args(self, args: list[str], timeout: float = 30, cwd: str | None = None) -> str:
        """Run a command without invoking a shell, return stdout."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        return await self._communicate(proc, timeout, args[0])

    async def _exec_shell(self, cmd_str: str, timeout: float = 30, cwd: str | None = None) -> str:
        """Run a legacy shell command, return stdout."""
        proc = await asyncio.create_subprocess_shell(
            cmd_str,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        return await self._communicate(proc, timeout, cmd_str)

    async def _communicate(self, proc, timeout: float, label: str) -> str:
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"Command timed out after {timeout}s: {label}")

        out = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")

        if proc.returncode != 0:
            raise RuntimeError(f"Exit code {proc.returncode}: {err or out}")
        return out

    @staticmethod
    def _parse_version(output: str) -> str:
        """Extract a version string from CLI output."""
        clean = output.strip()
        match = re.search(r"v?(\d+\.\d+(?:\.\d+)?(?:-[\w.]+)?)", clean, re.I)
        return match.group(1) if match else clean.split("\n")[0][:50]
