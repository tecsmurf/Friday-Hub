"""
Hermes Brain — Central orchestrator of Friday Hub.

ALL tasks flow through Hermes:
  1. Receive user input
  2. Check short-term + long-term memory
  3. Plan (single-step or multi-step via LLM planner)
  4. Gather project context for richer prompts
  5. Delegate to AI router → agent
  6. Store result in memory
  7. Learn from feedback
  8. Return result to user
"""

import asyncio
import logging
import time
import uuid

from friday.adapters.base import ExecResult
from friday.brain.planner import plan as plan_task
from friday.context.context_manager import get_context
from friday.health.monitor import HealthMonitor
from friday.learning.feedback import evaluate_result
from friday.memory import long_term, short_term
from friday.memory.logger import log_task
from friday.models import router as model_router
from friday.router.ai_router import get_fallbacks, route

log = logging.getLogger(__name__)


def _format_memory_context(similar: list[dict]) -> str | None:
    """Summarise past tasks for the router/planner, including their outcome."""
    lines = []
    for item in similar:
        meta = item.get("metadata") or {}
        prompt = meta.get("prompt")
        agent = meta.get("agent")
        if not prompt or not agent:
            continue
        success = meta.get("success")
        outcome = {"True": "succeeded", "False": "failed"}.get(str(success), "unknown outcome")
        if meta.get("user_rating"):
            outcome += f", user rated {meta['user_rating']}"
        lines.append(f"- {prompt} → {agent} ({outcome})")
    return "\n".join(lines) or None


class HermesBrain:
    """The central brain of Friday Hub."""

    def __init__(self, monitor: HealthMonitor | None = None):
        self.monitor = monitor or HealthMonitor()
        self._initialized = False

    async def initialize(self):
        """Detect all agents and run the version guard.

        The guard re-runs the smoke test when an agent's version changed (or
        it was previously incompatible), so a broken CLI update is excluded
        from routing before any task is sent to it.
        """
        await self.monitor.guard_all()
        self._initialized = True

    def _failure(self, task_id: str, user_input: str, start: float, output: str, **extra) -> dict:
        result = {
            "success": False,
            "output": output,
            "agent": extra.pop("agent", None),
            "duration": round(time.time() - start, 2),
            "routing": None,
            "task_id": task_id,
            **extra,
        }
        log_task(task_id, user_input, agent=result["agent"], success=False, result=output,
                 duration=result["duration"])
        return result

    async def think(self, user_input: str, forced_agent: str | None = None) -> dict:
        """
        Main entry point. Process a user task end-to-end.

        Flow:
          User input → Memory check → Plan → Route → Execute → Learn → Return
        """
        if not self._initialized:
            await self.initialize()

        task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        start = time.time()

        # ── Step 1: Store input in short-term memory ──
        short_term.add_to_conversation("user", user_input)
        short_term.put("current_task", user_input)

        # ── Step 2: Search long-term memory for context ──
        memory_context = None
        try:
            similar = long_term.search(user_input, n=3, tasks_only=True)
            memory_context = _format_memory_context(similar)
        except Exception as exc:
            log.info("Long-term memory search unavailable: %s", exc)

        # ── Step 2b: Gather project context ──
        project_context = None
        try:
            project_context = get_context().to_prompt_string()
        except Exception as exc:
            log.info("Project context unavailable: %s", exc)

        enriched_prompt = user_input
        if project_context:
            enriched_prompt = (
                f"{user_input}\n\n"
                f"--- Project Context ---\n{project_context}"
            )

        # ── Step 3: Work out which agents can take the task ──
        adapters = self.monitor.get_adapters()
        available = {
            aid: adapter.get_status()
            for aid, adapter in adapters.items()
            if self.monitor.is_usable(aid)
        }

        if forced_agent:
            if forced_agent not in adapters:
                known = ", ".join(adapters) or "none"
                return self._failure(
                    task_id, user_input, start,
                    f"Unknown agent: {forced_agent} (configured: {known})",
                    agent=forced_agent, error="Agent is not configured",
                )
            if forced_agent not in available:
                status = adapters[forced_agent].get_status()
                reason = status.error or "Agent is not installed or enabled"
                if self.monitor.version_guard.is_incompatible(forced_agent):
                    reason = "Marked incompatible by the version guard (run `friday doctor`)"
                return self._failure(
                    task_id, user_input, start,
                    f"Forced agent is not available: {forced_agent}",
                    agent=forced_agent, error=reason,
                )
            routing_decision = {
                "agent": forced_agent,
                "reason": "User forced this agent",
                "confidence": 1.0,
                "forced": True,
            }
            task_plan = None
        else:
            if not available:
                return self._failure(
                    task_id, user_input, start,
                    "No agents are available. Install at least one: claude, codex, or openclaw.",
                    error="No usable agents",
                )
            # Planning and routing are independent LLM calls — run them together.
            task_plan, routing_decision = await asyncio.gather(
                self._safe_plan(user_input, memory_context),
                route(user_input, available, memory_context),
            )
            self._apply_plan_hint(task_plan, routing_decision, available)

        # ── Step 4: Execute via adapter (with fallback unless forced) ──
        exec_result, attempts = await self._execute_with_fallback(
            routing_decision["agent"], enriched_prompt, available,
            allow_fallback=not forced_agent,
        )
        routing_decision["attempts"] = attempts

        duration = round(time.time() - start, 2)

        # ── Step 5: Store in memory ──
        short_term.add_to_conversation("assistant", exec_result.output[:500])
        short_term.put("last_result", {
            "task_id": task_id,
            "agent": exec_result.agent,
            "success": exec_result.success,
        })

        try:
            long_term.store(
                task_id=task_id,
                prompt=user_input,
                output=exec_result.output or exec_result.error or "",
                agent=exec_result.agent,
                metadata={
                    "success": str(exec_result.success),
                    "duration": str(duration),
                    "routing_confidence": str(routing_decision.get("confidence", 0)),
                },
            )
        except Exception as exc:
            log.info("Could not store task in long-term memory: %s", exc)

        # ── Step 6: Log + auto-evaluate ──
        log_task(
            task_id=task_id,
            prompt=user_input,
            routing_decision=routing_decision,
            agent=exec_result.agent,
            result=exec_result.output or exec_result.error,
            success=exec_result.success,
            duration=duration,
        )

        try:
            await evaluate_result(task_id, user_input, exec_result)
        except Exception as exc:
            log.info("Auto-evaluation failed: %s", exc)

        return {
            "success": exec_result.success,
            "output": exec_result.output,
            "agent": exec_result.agent,
            "agent_name": exec_result.agent_name,
            "version": exec_result.version,
            "duration": duration,
            "routing": routing_decision,
            "plan": task_plan,
            "task_id": task_id,
            "error": exec_result.error,
            "fallback_from": exec_result.fallback_from,
        }

    @staticmethod
    async def _safe_plan(user_input: str, memory_context: str | None) -> dict | None:
        try:
            return await plan_task(user_input, memory_context)
        except Exception as exc:
            log.info("Planning failed: %s", exc)
            return None

    @staticmethod
    def _apply_plan_hint(task_plan: dict | None, routing: dict, available: dict) -> None:
        """Record the planner's agent hint; follow it when the router was guessing."""
        steps = (task_plan or {}).get("steps") or []
        hinted = steps[0].get("agent_hint") if steps else None
        if hinted in (None, "any") or hinted not in available:
            return
        routing["plan_hint"] = hinted
        if hinted != routing["agent"] and routing.get("confidence", 0) < 0.5:
            routing["reason"] = f"{routing.get('reason', '')} — following planner hint ({hinted})".strip(" —")
            routing["agent"] = hinted

    async def _execute_with_fallback(
        self, agent_id: str, prompt: str, available: dict, allow_fallback: bool = True
    ) -> tuple[ExecResult, list[dict]]:
        """Execute on the chosen agent, then each usable configured fallback, once each.

        Returns the first successful result, or the primary agent's failure
        when every attempt failed. ``attempts`` records each try.
        """
        adapters = self.monitor.get_adapters()
        order = [agent_id]
        if allow_fallback:
            order += [fb for fb in get_fallbacks(agent_id) if fb not in order]

        attempts: list[dict] = []
        first_failure: ExecResult | None = None
        for aid in order:
            if aid not in available or aid not in adapters:
                continue
            result = await adapters[aid].execute(prompt)
            attempts.append({"agent": aid, "success": result.success, "error": result.error})
            if result.success:
                if aid != agent_id:
                    result.fallback_from = agent_id
                return result, attempts
            first_failure = first_failure or result

        if first_failure is not None:
            if len(attempts) > 1:
                tried = ", ".join(a["agent"] for a in attempts)
                first_failure.error = f"{first_failure.error} (also tried: {tried})"
            return first_failure, attempts

        return ExecResult(
            success=False, output="", duration=0,
            agent=agent_id, agent_name=agent_id,
            error="Agent not available",
        ), attempts

    async def get_status(self) -> dict:
        """Get full system status."""
        model_status, health_results = await asyncio.gather(
            model_router.get_provider_status(),
            self.monitor.check_all(),
        )
        self._initialized = True

        return {
            "models": model_status,
            "agents": health_results,
            "memory": {
                "short_term": len(short_term.get_recent(100)),
                "long_term": long_term.count(),
            },
        }
