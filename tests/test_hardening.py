"""Regression tests for defects fixed in the audit (see docs/ENGINEERING_LOG.md)."""

import json
import sys
import textwrap
from pathlib import Path

import pytest

from friday.adapters.base import AdapterStatus, BaseAdapter, ExecResult

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX shell scripts as fake CLIs")


def _fake_cli(tmp_path: Path, name: str, body: str) -> str:
    """Write an executable Python script acting as an agent CLI."""
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)
    return str(path)


# ── Adapters ─────────────────────────────────────────────────────────


@posix_only
class TestPromptDelivery:
    @pytest.mark.asyncio
    async def test_prompt_via_stdin_when_no_placeholder(self, tmp_path):
        cmd = _fake_cli(tmp_path, "agent", """
            import sys
            print("STDIN:" + sys.stdin.read())
        """)
        adapter = BaseAdapter("a", {"command": cmd, "execute_args": ["-p"]})
        result = await adapter.execute("hello $(whoami) `id` \"q\" [red]")
        assert result.success, result.error
        assert result.output == "STDIN:hello $(whoami) `id` \"q\" [red]"

    @pytest.mark.asyncio
    async def test_prompt_file_placeholder(self, tmp_path):
        cmd = _fake_cli(tmp_path, "agent", """
            import sys
            print(open(sys.argv[2], encoding="utf-8").read())
        """)
        adapter = BaseAdapter("a", {"command": cmd, "execute_args": ["--message-file", "{prompt_file}"]})
        result = await adapter.execute("multi\nline ✓")
        assert result.success, result.error
        assert result.output == "multi\nline ✓"

    @pytest.mark.asyncio
    async def test_legacy_pattern_never_uses_a_shell(self, tmp_path):
        """A legacy execute_pattern used to run via the shell: $(...) executed."""
        marker = tmp_path / "pwned"
        cmd = _fake_cli(tmp_path, "agent", """
            import sys
            print(repr(sys.argv[1:]))
        """)
        adapter = BaseAdapter("a", {"command": cmd, "execute_pattern": '{cmd} -p "{prompt}"'})
        prompt = f'x" ; touch {marker} ; echo "$(touch {marker})'
        result = await adapter.execute(prompt)
        assert result.success, result.error
        assert not marker.exists()
        assert result.output == repr(["-p", prompt])

    @pytest.mark.asyncio
    async def test_prompt_containing_placeholders_is_not_resubstituted(self, tmp_path):
        cmd = _fake_cli(tmp_path, "agent", "import sys; print(sys.argv[1])\n")
        adapter = BaseAdapter("a", {"command": cmd, "execute_args": ["{prompt}"]})
        result = await adapter.execute("use {cmd} and {prompt_file}")
        assert result.output == "use {cmd} and {prompt_file}"

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, tmp_path):
        cmd = _fake_cli(tmp_path, "agent", "import time; time.sleep(30)\n")
        adapter = BaseAdapter("a", {"command": cmd, "execute_args": ["{prompt}"], "timeout": 0.5})
        result = await adapter.execute("x")
        assert not result.success
        assert "timed out" in result.error
        assert result.duration < 5


@posix_only
class TestHealth:
    @pytest.mark.asyncio
    async def test_failing_health_command_is_unhealthy(self, tmp_path):
        """Health failures used to be swallowed and reported as healthy."""
        cmd = _fake_cli(tmp_path, "agent", """
            import sys
            if "--version" in sys.argv: print("agent 1.2.3"); sys.exit(0)
            print("error: unknown option", file=sys.stderr); sys.exit(1)
        """)
        adapter = BaseAdapter("a", {"command": cmd, "health_args": ["--doctor"]})
        health = await adapter.health_check()
        assert health["healthy"] is False
        assert "unknown option" in health["message"]
        assert adapter.get_status().healthy is False

    @pytest.mark.asyncio
    async def test_version_guard_persists_incompatibility_and_recovers(self, tmp_path):
        from friday.health.version_guard import VersionGuard

        state = tmp_path / "healthy"
        cmd = _fake_cli(tmp_path, "agent", f"""
            import os, sys
            if "--version" in sys.argv:
                print(open({str(tmp_path / 'version')!r}).read()); sys.exit(0)
            sys.exit(0 if os.path.exists({str(state)!r}) else 1)
        """)
        (tmp_path / "version").write_text("1.0.0")
        adapter = BaseAdapter("a", {"command": cmd, "health_args": ["--help"]})
        guard = VersionGuard(lock_path=tmp_path / "lock.json")

        assert (await guard.guard_all({"a": adapter}))["a"]["status"] == "new"

        (tmp_path / "version").write_text("2.0.0")  # update breaks the CLI
        assert (await guard.guard_all({"a": adapter}))["a"]["status"] == "incompatible"
        # Same version on the next run must stay incompatible (was forgotten before).
        reloaded = VersionGuard(lock_path=tmp_path / "lock.json")
        assert reloaded.is_incompatible("a")
        assert (await reloaded.guard_all({"a": adapter}))["a"]["status"] == "incompatible"

        state.write_text("ok")  # CLI fixed
        assert (await reloaded.guard_all({"a": adapter}))["a"]["status"] == "recovered"
        assert not reloaded.is_incompatible("a")


def test_shipped_agent_configs_use_non_interactive_commands():
    """codex needs `exec`; openclaw has no `run`; claude has no `--doctor`."""
    from friday.config import load_config, DEFAULT_CONFIG_PATH

    for path in (DEFAULT_CONFIG_PATH, Path(__file__).parent.parent / "friday.yaml"):
        agents = load_config(path)["agents"]
        assert agents["codex"]["execute_args"][0] == "exec"
        assert agents["openclaw"]["execute_args"][0] == "agent"
        assert "--doctor" not in (agents["claude-code"]["health_args"] or [])


# ── Hermes routing & fallback ────────────────────────────────────────


class FakeAdapter:
    def __init__(self, aid, succeed=True, priority=1, installed=True):
        self.id, self.name, self.calls = aid, aid, 0
        self.succeed, self.priority, self.installed = succeed, priority, installed

    async def execute(self, prompt):
        self.calls += 1
        return ExecResult(success=self.succeed, output="ok" if self.succeed else "",
                          duration=0, agent=self.id, agent_name=self.id,
                          error=None if self.succeed else f"{self.id} broke")

    def get_status(self):
        return AdapterStatus(id=self.id, name=self.name, enabled=True,
                             installed=self.installed, priority=self.priority)


class FakeGuard:
    def __init__(self, incompatible=()):
        self.incompatible = set(incompatible)

    def is_incompatible(self, aid):
        return aid in self.incompatible


class FakeMonitor:
    def __init__(self, adapters, incompatible=()):
        self.adapters = adapters
        self.version_guard = FakeGuard(incompatible)

    async def guard_all(self):
        return {}

    def get_adapters(self):
        return self.adapters

    def is_usable(self, aid):
        a = self.adapters.get(aid)
        return bool(a and a.installed) and not self.version_guard.is_incompatible(aid)


@pytest.fixture
def no_llm(monkeypatch):
    """Make routing/planning/memory deterministic and offline."""
    from friday.brain import hermes

    async def fake_route(task, available, memory_context=None):
        return {"agent": next(iter(available)), "reason": "test", "confidence": 0.9}

    async def fake_plan(task, memory_context=None):
        return None

    monkeypatch.setattr(hermes, "route", fake_route)
    monkeypatch.setattr(hermes, "plan_task", fake_plan)
    monkeypatch.setattr(hermes.long_term, "search", lambda *a, **k: [])
    monkeypatch.setattr(hermes.long_term, "store", lambda *a, **k: None)
    monkeypatch.setattr(hermes, "get_fallbacks", lambda aid: ["b", "c"] if aid == "a" else [])
    return hermes


class TestHermes:
    @pytest.mark.asyncio
    async def test_all_failed_does_not_rerun_primary(self, no_llm):
        """The primary agent used to be executed a second time after all fallbacks failed."""
        a, b = FakeAdapter("a", succeed=False), FakeAdapter("b", succeed=False)
        brain = no_llm.HermesBrain(monitor=FakeMonitor({"a": a, "b": b}))
        result = await brain.think("do it")
        assert not result["success"]
        assert (a.calls, b.calls) == (1, 1)
        assert "a broke" in result["error"]

    @pytest.mark.asyncio
    async def test_fallback_success(self, no_llm):
        a, b = FakeAdapter("a", succeed=False), FakeAdapter("b")
        brain = no_llm.HermesBrain(monitor=FakeMonitor({"a": a, "b": b}))
        result = await brain.think("do it")
        assert result["success"] and result["agent"] == "b" and result["fallback_from"] == "a"

    @pytest.mark.asyncio
    async def test_forced_agent_does_not_fall_back(self, no_llm):
        a, b = FakeAdapter("a", succeed=False), FakeAdapter("b")
        brain = no_llm.HermesBrain(monitor=FakeMonitor({"a": a, "b": b}))
        result = await brain.think("do it", forced_agent="a")
        assert not result["success"] and b.calls == 0

    @pytest.mark.asyncio
    async def test_incompatible_agent_is_skipped(self, no_llm):
        a, b = FakeAdapter("a"), FakeAdapter("b")
        brain = no_llm.HermesBrain(monitor=FakeMonitor({"a": a, "b": b}, incompatible={"a"}))
        result = await brain.think("do it")
        assert result["agent"] == "b" and a.calls == 0

    @pytest.mark.asyncio
    async def test_no_agents(self, no_llm):
        brain = no_llm.HermesBrain(monitor=FakeMonitor({"a": FakeAdapter("a", installed=False)}))
        result = await brain.think("do it")
        assert not result["success"] and "No agents" in result["output"]


class TestRouter:
    @pytest.mark.asyncio
    async def test_unavailable_default_uses_best_available(self, monkeypatch):
        """Router used to return the default agent even when it wasn't installed."""
        from friday.router import ai_router

        async def broken_chat(**kwargs):
            raise RuntimeError("no llm")

        monkeypatch.setattr(ai_router.model_router, "chat", broken_chat)
        available = {
            "openclaw": AdapterStatus(id="openclaw", name="OC", enabled=True, installed=True, priority=3),
            "codex": AdapterStatus(id="codex", name="Codex", enabled=True, installed=True, priority=2),
        }
        decision = await ai_router.route("write tests", available)
        assert decision["agent"] == "codex"

    @pytest.mark.asyncio
    async def test_model_picks_unknown_agent(self, monkeypatch):
        from friday.router import ai_router

        async def chat(**kwargs):
            return {"content": '<think>hmm</think>```json\n{"agent": "skynet", "confidence": "high"}\n```'}

        monkeypatch.setattr(ai_router.model_router, "chat", chat)
        available = {
            "codex": AdapterStatus(id="codex", name="Codex", enabled=True, installed=True, priority=2),
            "openclaw": AdapterStatus(id="openclaw", name="OC", enabled=True, installed=True, priority=3),
        }
        decision = await ai_router.route("x", available)
        assert decision["agent"] == "codex"


class TestParsing:
    @pytest.mark.parametrize("content", [
        '{"a": 1}',
        'Sure! {"a": 1} hope that helps',
        '```json\n{"a": 1}\n```',
        '<think>{"not": "this"}</think>\n{"a": 1}',
    ])
    def test_parse_json_object(self, content):
        from friday.models.parsing import parse_json_object

        assert parse_json_object(content) == {"a": 1}

    def test_parse_json_object_fails_clearly(self):
        from friday.models.parsing import parse_json_object

        with pytest.raises(ValueError):
            parse_json_object("no json here")


# ── Models ───────────────────────────────────────────────────────────


class TestOllamaHost:
    @pytest.mark.parametrize("raw,expected", [
        ("", "http://localhost:11434"),
        ("localhost", "http://localhost:11434"),
        ("0.0.0.0", "http://localhost:11434"),
        ("10.0.0.0:8080", "http://10.0.0.0:8080"),
        ("https://ollama.example.com:443/", "https://ollama.example.com:443"),
    ])
    def test_get_host(self, monkeypatch, raw, expected):
        from friday.models import ollama_client

        monkeypatch.setenv("OLLAMA_HOST", raw)
        assert ollama_client.get_host() == expected


@pytest.mark.asyncio
async def test_model_router_reports_each_provider_error(monkeypatch):
    from friday.models import router

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    async def down():
        return False

    monkeypatch.setattr(router.ollama_client, "is_available", down)
    with pytest.raises(RuntimeError) as err:
        await router.chat([{"role": "user", "content": "hi"}])
    assert "ollama: not reachable" in str(err.value)
    assert "OPENROUTER_API_KEY not set" in str(err.value)


# ── Logs, feedback, CLI ──────────────────────────────────────────────


class TestLogger:
    def test_task_id_cannot_escape_log_dir(self):
        from friday.memory import logger

        with pytest.raises(ValueError):
            logger.log_task("../../evil", "x")
        assert logger.add_feedback("../../evil", "good") is False

    def test_add_feedback(self):
        from friday.memory import logger

        path = logger.log_task("task_1_abc", "prompt", success=True)
        assert logger.add_feedback("task_1_abc", "bad", "wrong file")
        assert json.loads(path.read_text())["feedback"] == {"rating": "bad", "comment": "wrong file"}
        assert logger.add_feedback("task_missing", "bad") is False


class TestCli:
    def test_run_without_task_is_usage_error(self):
        from typer.testing import CliRunner
        from friday.cli import app

        result = CliRunner().invoke(app, ["run"])
        assert result.exit_code == 2

    def test_failed_task_exits_nonzero_and_output_is_literal(self, monkeypatch):
        from typer.testing import CliRunner
        from friday import cli
        from friday.brain import hermes

        class Brain:
            async def initialize(self):
                pass

            async def think(self, prompt, forced_agent=None):
                return {"success": False, "output": "boom [/bold] arr[i]", "agent": "codex",
                        "error": "Exit code 1", "task_id": "task_1_x"}

        monkeypatch.setattr(hermes, "HermesBrain", Brain)
        result = CliRunner().invoke(cli.app, ["run", "x"])
        assert result.exit_code == 1
        assert "boom [/bold] arr[i]" in result.output  # markup not interpreted, no crash

    def test_successful_output_with_markup_like_text(self, monkeypatch):
        from typer.testing import CliRunner
        from friday import cli
        from friday.brain import hermes

        class Brain:
            async def initialize(self):
                pass

            async def think(self, prompt, forced_agent=None):
                return {"success": True, "output": "x = arr[i] [/] done", "agent": "codex",
                        "agent_name": "Codex", "version": None, "duration": 1, "routing": {}}

        monkeypatch.setattr(hermes, "HermesBrain", Brain)
        result = CliRunner().invoke(cli.app, ["run", "x"])
        assert result.exit_code == 0, result.output
        assert "x = arr[i] [/] done" in result.output
        assert "vNone" not in result.output


def test_context_tree_is_bounded(tmp_path):
    from friday.context.context_manager import ContextManager

    for i in range(120):
        d = tmp_path / f"dir{i}"
        d.mkdir()
        (d / "f.py").write_text("x")
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "secret").write_text("x")
    (tmp_path / "pkg.egg-info").mkdir()

    entries, total = ContextManager(tmp_path)._build_file_tree()
    assert len(entries) <= 50
    assert total == 120
    assert not any(".cache" in e or "egg-info" in e for e in entries)


def test_short_term_recency_with_identical_timestamps(monkeypatch):
    """Windows' coarse clock gave equal timestamps, so get_recent returned the oldest first."""
    from friday.memory import short_term

    short_term.clear()
    monkeypatch.setattr(short_term.time, "time", lambda: 1000.0)
    for key in ("a", "b", "c"):
        short_term.put(key, key)
    assert [e["key"] for e in short_term.get_recent(2)] == ["c", "b"]
    short_term.clear()
