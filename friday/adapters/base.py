"""
Base adapter for all external agent CLIs.

Each concrete adapter wraps a third-party command behind the same interface so
the router can execute tasks without knowing the CLI details.

Commands are always run without a shell. The prompt reaches the agent in one
of three ways, chosen by the placeholders in ``execute_args``:

* ``{prompt}``      — substituted into a single argv element
* ``{prompt_file}`` — written to a temporary UTF-8 file whose path is passed
* neither           — written to the process's stdin

Stdin and file delivery avoid OS command-line length limits (project context
can make prompts long) and keep the prompt away from ``cmd.exe`` parsing when
the agent is an npm ``.cmd`` shim on Windows.
"""

import asyncio
import contextlib
import logging
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger(__name__)

DEFAULT_EXECUTE_TIMEOUT = 600.0
_DETECT_TIMEOUT = 10.0
_HEALTH_TIMEOUT = 15.0


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
        self.timeout = float(cfg.get("timeout") or DEFAULT_EXECUTE_TIMEOUT)

        # Legacy string patterns are still accepted for custom user configs.
        # They are tokenised with shlex and run without a shell.
        self.execute_pattern = cfg.get("execute_pattern", "")
        self.version_command = cfg.get("version_command", "")
        self.health_command = cfg.get("health_command")
        self.priority = cfg.get("priority", 99)

        self._version: str | None = None
        self._installed: bool | None = None
        self._healthy: bool | None = None
        self._last_check: datetime | None = None
        self._error: str | None = None

    # ── Public API ──────────────────────────────────────────────────

    async def detect(self) -> dict:
        """Check whether the CLI is installed and return version info."""
        if not self._resolve_executable():
            self._installed = False
            self._error = f"'{self.command}' not found on PATH"
            return {"installed": False, "version": None, "error": self._error}

        try:
            output = await self._run(self._version_argv(), timeout=_DETECT_TIMEOUT)
            version = self._parse_version(output)
            self._version = version
            self._installed = True
            self._error = None
            return {"installed": True, "version": version, "error": None}
        except Exception as exc:
            self._installed = False
            self._error = str(exc)
            return {"installed": False, "version": None, "error": str(exc)}

    async def health_check(self, refresh: bool = True) -> dict:
        """Check the CLI is installed and its health command succeeds.

        Args:
            refresh: Re-run detection first. Pass False when ``detect()`` has
                just been called to avoid spawning the version command again.
        """
        if not self.enabled:
            return {"healthy": False, "message": "Disabled in config"}

        if refresh or self._installed is None:
            await self.detect()
        self._last_check = datetime.now()

        if not self._installed:
            self._healthy = False
            return {"healthy": False, "message": f"Not installed: {self._error}"}

        argv = self._health_argv()
        if argv is not None:
            try:
                await self._run(argv, timeout=_HEALTH_TIMEOUT)
            except Exception as exc:
                self._healthy = False
                self._error = f"Health check failed: {exc}"
                return {"healthy": False, "message": self._error}

        self._healthy = True
        return {"healthy": True, "message": f"OK (v{self._version})"}

    async def execute(self, prompt: str, cwd: str | None = None, timeout: float | None = None) -> ExecResult:
        """Execute a task via the agent CLI."""
        if not self.enabled:
            raise RuntimeError(f"{self.name} is disabled")

        timeout = timeout if timeout is not None else self.timeout
        loop = asyncio.get_running_loop()
        start = loop.time()
        prompt_file: str | None = None
        try:
            template = self._execute_template()
            stdin_data: bytes | None = None
            if any("{prompt_file}" in arg for arg in template):
                prompt_file = self._write_prompt_file(prompt)
            elif not any("{prompt}" in arg for arg in template):
                stdin_data = prompt.encode("utf-8")

            argv = self._format_args(template, prompt=prompt, prompt_file=prompt_file)
            output = await self._run(argv, timeout=timeout, cwd=cwd, stdin_data=stdin_data)
            return ExecResult(
                success=True,
                output=output.strip(),
                duration=round(loop.time() - start, 2),
                agent=self.id,
                agent_name=self.name,
                version=self._version,
            )
        except Exception as exc:
            log.debug("%s execution failed: %s", self.id, exc)
            return ExecResult(
                success=False,
                output="",
                duration=round(loop.time() - start, 2),
                agent=self.id,
                agent_name=self.name,
                version=self._version,
                error=str(exc),
            )
        finally:
            if prompt_file:
                with contextlib.suppress(OSError):
                    os.unlink(prompt_file)

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

    # ── Argument building ───────────────────────────────────────────

    def _execute_template(self) -> list[str]:
        if self.execute_args is not None:
            return [self.command, *map(str, self.execute_args)]
        if self.execute_pattern:
            return self._split_pattern(self.execute_pattern)
        return [self.command, "{prompt}"]

    def _version_argv(self) -> list[str]:
        if self.version_args is not None:
            return [self.command, *self._format_args(self.version_args)]
        if self.version_command:
            return self._format_args(self._split_pattern(self.version_command))
        return [self.command, "--version"]

    def _health_argv(self) -> list[str] | None:
        if self.health_args is not None:
            return [self.command, *self._format_args(self.health_args)]
        if self.health_command:
            return self._format_args(self._split_pattern(self.health_command))
        return None

    @staticmethod
    def _split_pattern(pattern: str) -> list[str]:
        """Tokenise a legacy ``"{cmd} -p \\"{prompt}\\""`` pattern into argv.

        Placeholders are substituted per token *after* splitting, so the prompt
        always stays a single argument and is never seen by a shell.
        """
        return shlex.split(pattern, posix=True)

    def _format_args(self, args: list, prompt: str | None = None, prompt_file: str | None = None) -> list[str]:
        replacements = {
            "{cmd}": self.command,
            "{prompt_file}": prompt_file or "",
            "{prompt}": prompt or "",
        }
        formatted = []
        for arg in args:
            value = str(arg)
            # Single pass so text inside the prompt is never re-substituted.
            value = re.sub(
                r"\{cmd\}|\{prompt_file\}|\{prompt\}",
                lambda m: replacements[m.group(0)],
                value,
            )
            formatted.append(value)
        return formatted

    @staticmethod
    def _write_prompt_file(prompt: str) -> str:
        fd, path = tempfile.mkstemp(prefix="friday_prompt_", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(prompt)
        return path

    def _resolve_executable(self) -> str | None:
        """Full path of the command, honouring PATHEXT (``.cmd`` shims) on Windows."""
        return shutil.which(self.command)

    # ── Process execution ───────────────────────────────────────────

    async def _run(
        self,
        argv: list[str],
        timeout: float = 30,
        cwd: str | None = None,
        stdin_data: bytes | None = None,
    ) -> str:
        """Run a command without a shell and return stdout.

        Raises RuntimeError on a non-zero exit and TimeoutError on timeout.
        The whole process group is killed on timeout or cancellation so agent
        sub-processes do not outlive Friday.
        """
        executable = shutil.which(argv[0]) or argv[0]
        kwargs: dict = {}
        if sys.platform != "win32":
            kwargs["start_new_session"] = True

        proc = await asyncio.create_subprocess_exec(
            executable,
            *argv[1:],
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            **kwargs,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(stdin_data), timeout=timeout)
        except asyncio.TimeoutError:
            await self._kill(proc)
            raise TimeoutError(f"Command timed out after {timeout:g}s: {argv[0]}") from None
        except asyncio.CancelledError:
            await self._kill(proc)
            raise

        out = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")

        if proc.returncode != 0:
            detail = (err or out).strip()
            if len(detail) > 2000:
                detail = detail[-2000:]
            raise RuntimeError(f"Exit code {proc.returncode}: {detail}")
        return out

    @staticmethod
    async def _kill(proc) -> None:
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            if sys.platform != "win32":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()

    @staticmethod
    def _parse_version(output: str) -> str:
        """Extract a version string from CLI output."""
        clean = output.strip()
        match = re.search(r"v?(\d+\.\d+(?:\.\d+)?(?:-[\w.]+)?)", clean, re.I)
        return match.group(1) if match else clean.split("\n")[0][:50]
