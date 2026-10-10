"""Tests for friday.learning.feedback — Auto and manual feedback scoring."""

from dataclasses import dataclass

import pytest


@dataclass
class MockExecResult:
    """Minimal mock of adapters.base.ExecResult for testing."""
    success: bool
    output: str
    duration: float
    agent: str
    agent_name: str
    version: str | None = None
    error: str | None = None
    fallback_from: str | None = None


class TestAutoEvaluate:
    """Test the heuristic auto-evaluation scoring."""

    @pytest.mark.asyncio
    async def test_substantial_output_high_score(self):
        """Successful task with substantial output should score ~0.9."""
        from friday.learning.feedback import evaluate_result

        result = MockExecResult(
            success=True,
            output="x" * 600,  # > 500 chars
            duration=1.5,
            agent="claude-code",
            agent_name="Claude Code",
        )
        feedback = await evaluate_result("test_1", "refactor module", result)

        assert feedback is not None
        assert feedback["score"] == 0.9
        assert feedback["agent"] == "claude-code"
        assert feedback["auto"] is True

    @pytest.mark.asyncio
    async def test_moderate_output_medium_score(self):
        """Successful task with moderate output should score ~0.7."""
        from friday.learning.feedback import evaluate_result

        result = MockExecResult(
            success=True,
            output="x" * 200,  # 100 < len < 500
            duration=1.0,
            agent="codex",
            agent_name="Codex CLI",
        )
        feedback = await evaluate_result("test_2", "write test", result)

        assert feedback is not None
        assert feedback["score"] == 0.7

    @pytest.mark.asyncio
    async def test_brief_output_low_score(self):
        """Successful task with brief output should score ~0.5."""
        from friday.learning.feedback import evaluate_result

        result = MockExecResult(
            success=True,
            output="OK",  # < 100 chars
            duration=0.5,
            agent="codex",
            agent_name="Codex CLI",
        )
        feedback = await evaluate_result("test_3", "lint file", result)

        assert feedback is not None
        assert feedback["score"] == 0.5

    @pytest.mark.asyncio
    async def test_empty_output_minimal_score(self):
        """Successful but empty output should score 0.3."""
        from friday.learning.feedback import evaluate_result

        result = MockExecResult(
            success=True,
            output="",
            duration=0.1,
            agent="codex",
            agent_name="Codex CLI",
        )
        feedback = await evaluate_result("test_4", "no-op", result)

        assert feedback is not None
        assert feedback["score"] == 0.3

    @pytest.mark.asyncio
    async def test_failure_lowest_score(self):
        """Failed task should score 0.1."""
        from friday.learning.feedback import evaluate_result

        result = MockExecResult(
            success=False,
            output="",
            duration=2.0,
            agent="openclaw",
            agent_name="OpenClaw",
            error="Command timed out",
        )
        feedback = await evaluate_result("test_5", "broken task", result)

        assert feedback is not None
        assert feedback["score"] == 0.1
        assert "timed out" in feedback["reason"]

    @pytest.mark.asyncio
    async def test_feedback_includes_task_id(self):
        """Feedback should include the correct task_id."""
        from friday.learning.feedback import evaluate_result

        result = MockExecResult(
            success=True,
            output="done",
            duration=0.5,
            agent="claude-code",
            agent_name="Claude Code",
        )
        feedback = await evaluate_result("my_task_123", "do thing", result)

        assert feedback is not None
        assert feedback["task_id"] == "my_task_123"
