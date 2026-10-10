"""Tests for friday.adapters — Base adapter behavior."""


import pytest


class TestAdapterStatus:
    """Test the AdapterStatus dataclass."""

    def test_default_values(self):
        """AdapterStatus should have sensible defaults."""
        from friday.adapters.base import AdapterStatus

        status = AdapterStatus(id="test", name="Test Agent", enabled=True)
        assert status.id == "test"
        assert status.installed is None
        assert status.healthy is None
        assert status.version is None
        assert status.priority == 99
        assert status.capabilities == []

    def test_full_status(self):
        """AdapterStatus should accept all fields."""
        from friday.adapters.base import AdapterStatus

        status = AdapterStatus(
            id="claude-code",
            name="Claude Code",
            enabled=True,
            installed=True,
            healthy=True,
            version="1.2.3",
            priority=1,
            capabilities=["debugging", "refactoring"],
        )
        assert status.version == "1.2.3"
        assert status.capabilities == ["debugging", "refactoring"]


class TestExecResult:
    """Test the ExecResult dataclass."""

    def test_success_result(self):
        """Successful ExecResult should have success=True."""
        from friday.adapters.base import ExecResult

        result = ExecResult(
            success=True,
            output="refactored 3 files",
            duration=2.5,
            agent="claude-code",
            agent_name="Claude Code",
            version="1.0.0",
        )
        assert result.success is True
        assert result.error is None
        assert result.fallback_from is None

    def test_failure_result(self):
        """Failed ExecResult should carry error info."""
        from friday.adapters.base import ExecResult

        result = ExecResult(
            success=False,
            output="",
            duration=0.1,
            agent="codex",
            agent_name="Codex CLI",
            error="Exit code 1: command not found",
        )
        assert result.success is False
        assert "Exit code" in result.error

    def test_fallback_result(self):
        """ExecResult with fallback_from tracks the original agent."""
        from friday.adapters.base import ExecResult

        result = ExecResult(
            success=True,
            output="done",
            duration=1.0,
            agent="codex",
            agent_name="Codex CLI",
            fallback_from="claude-code",
        )
        assert result.fallback_from == "claude-code"


class TestBaseAdapter:
    """Test BaseAdapter initialization and utilities."""

    def _make_adapter(self, **overrides):
        """Create a BaseAdapter with test config."""
        from friday.adapters.base import BaseAdapter

        cfg = {
            "name": "Test Agent",
            "command": "test-cmd",
            "enabled": True,
            "priority": 2,
            "capabilities": ["testing", "mocking"],
            "execute_args": ["-p", "{prompt}"],
            "version_args": ["--version"],
            "health_args": None,
            **overrides,
        }
        return BaseAdapter("test-agent", cfg)

    def test_init_from_config(self):
        """Adapter should read all fields from config dict."""
        adapter = self._make_adapter()
        assert adapter.id == "test-agent"
        assert adapter.name == "Test Agent"
        assert adapter.command == "test-cmd"
        assert adapter.enabled is True
        assert adapter.priority == 2
        assert "testing" in adapter.capabilities

    def test_get_status_initial(self):
        """Initial status should have None for install/health/version."""
        adapter = self._make_adapter()
        status = adapter.get_status()
        assert status.id == "test-agent"
        assert status.installed is None
        assert status.healthy is None
        assert status.version is None

    def test_format_args_with_prompt(self):
        """_format_args should replace {prompt} placeholder."""
        adapter = self._make_adapter()
        formatted = adapter._format_args(["-p", "{prompt}"], prompt="hello world")
        assert formatted == ["-p", "hello world"]

    def test_format_args_with_cmd(self):
        """_format_args should replace {cmd} placeholder."""
        adapter = self._make_adapter()
        formatted = adapter._format_args(["{cmd}", "--version"])
        assert formatted == ["test-cmd", "--version"]

    def test_parse_version_semver(self):
        """_parse_version should extract semver strings."""
        from friday.adapters.base import BaseAdapter

        assert BaseAdapter._parse_version("v1.2.3") == "1.2.3"
        assert BaseAdapter._parse_version("Version 2.0.1-beta") == "2.0.1-beta"
        assert BaseAdapter._parse_version("claude 1.0.5 (build 123)") == "1.0.5"

    def test_parse_version_no_match(self):
        """_parse_version should return first line if no version pattern."""
        from friday.adapters.base import BaseAdapter

        assert BaseAdapter._parse_version("no version here") == "no version here"

    @pytest.mark.asyncio
    async def test_detect_not_on_path(self):
        """detect() should report not installed when command is not on PATH."""
        adapter = self._make_adapter(command="nonexistent-binary-xyz")
        result = await adapter.detect()
        assert result["installed"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_disabled_adapter(self):
        """execute() should raise when adapter is disabled."""
        adapter = self._make_adapter(enabled=False)
        with pytest.raises(RuntimeError, match="disabled"):
            await adapter.execute("do something")


class TestConcreteAdapters:
    """Test that concrete adapters initialize correctly."""

    def test_claude_code_adapter(self):
        """ClaudeCodeAdapter should set id='claude-code'."""
        from friday.adapters.claude_code import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter({"name": "Claude Code", "command": "claude"})
        assert adapter.id == "claude-code"
        assert adapter.name == "Claude Code"

    def test_codex_adapter(self):
        """CodexAdapter should set id='codex'."""
        from friday.adapters.codex import CodexAdapter

        adapter = CodexAdapter({"name": "Codex CLI", "command": "codex"})
        assert adapter.id == "codex"

    def test_openclaw_adapter(self):
        """OpenClawAdapter should set id='openclaw'."""
        from friday.adapters.openclaw import OpenClawAdapter

        adapter = OpenClawAdapter({"name": "OpenClaw", "command": "openclaw"})
        assert adapter.id == "openclaw"
