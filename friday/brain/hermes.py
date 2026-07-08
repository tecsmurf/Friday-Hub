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

import time
import uuid
from friday.models import router as model_router
from friday.memory import short_term, long_term
from friday.memory.logger import log_task
from friday.router.ai_router import route, get_fallbacks
from friday.health.monitor import HealthMonitor
from friday.learning.feedback import evaluate_result
from friday.brain.planner import plan as plan_task
from friday.context.context_manager import get_context


class HermesBrain:
    """The central brain of Friday Hub."""

    def __init__(self):
        self.monitor = HealthMonitor()
        self._initialized = False

    async def initialize(self):
        """Detect all agents."""
        await self.monitor.detect_all()
        self._initialized = True

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
        short_term.put(f"current_task", user_input)

        # ── Step 2: Search long-term memory for context ──
        memory_context = None
        try:
            similar = long_term.search(user_input, n=3)
            if similar:
                memory_context = "\n".join(
                    f"- {s['metadata'].get('prompt', '')} → {s['metadata'].get('agent', '')}"
                    for s in similar
                )
        except Exception:
            pass

        # ── Step 2b: Gather project context ──
        project_context = None
        try:
            ctx = get_context()
            project_context = ctx.to_prompt_string()
        except Exception:
            pass

        # ── Step 2c: Plan if task is complex ──
        task_plan = None
        try:
            task_plan = await plan_task(user_input, memory_context)
        except Exception:
            pass

        # Enrich the prompt with context if available
        enriched_prompt = user_input
        if project_context:
            enriched_prompt = (
                f"{user_input}\n\n"
                f"--- Project Context ---\n{project_context}"
            )

        # ── Step 3: Route to best agent ──
        adapters = self.monitor.get_adapters()
        statuses = {aid: a.get_status() for aid, a in adapters.items()}

        # Filter to only available agents
        available = {aid: s for aid, s in statuses.items() if s.installed and s.enabled}

        if not available:
            result = {
                "success": False,
                "output": "No agents are available. Install at least one: claude, codex, or openclaw.",
                "agent": None,
                "duration": round(time.time() - start, 2),
                "routing": None,
            }
            log_task(task_id, user_input, success=False, result=result["output"])
            return result

        if forced_agent:
            if forced_agent not in adapters:
                result = {
                    "success": False,
                    "output": f"Unknown agent: {forced_agent}",
                    "agent": forced_agent,
                    "duration": round(time.time() - start, 2),
                    "routing": None,
                    "error": "Agent is not configured",
                }
                log_task(task_id, user_input, success=False, result=result["output"])
                return result

            forced_status = statuses[forced_agent]
            if not forced_status.installed or not forced_status.enabled:
                result = {
                    "success": False,
                    "output": f"Forced agent is not available: {forced_agent}",
                    "agent": forced_agent,
                    "duration": round(time.time() - start, 2),
                    "routing": None,
                    "error": forced_status.error or "Agent is not installed or enabled",
                }
                log_task(task_id, user_input, success=False, result=result["output"])
                return result

            routing_decision = {
                "agent": forced_agent,
                "reason": "User forced this agent",
                "confidence": 1.0,
                "forced": True,
            }
        else:
            routing_decision = await route(user_input, available, memory_context)
        agent_id = routing_decision["agent"]

        # If planner provided an agent hint, consider it
        if task_plan and not forced_agent:
            steps = task_plan.get("steps", [])
            if steps and steps[0].get("agent_hint") not in ("any", None):
                hinted = steps[0]["agent_hint"]
                if hinted in available:
                    routing_decision["plan_hint"] = hinted

        # ── Step 4: Execute via adapter (with fallback) ──
        exec_result = await self._execute_with_fallback(
            agent_id, enriched_prompt, adapters, routing_decision
        )

        duration = round(time.time() - start, 2)

        # ── Step 5: Store in memory ──
        short_term.add_to_conversation("assistant", exec_result.output[:500])
        short_term.put(f"last_result", {
            "task_id": task_id,
            "agent": exec_result.agent,
            "success": exec_result.success,
        })

        try:
            long_term.store(
                task_id=task_id,
                prompt=user_input,
                output=exec_result.output,
                agent=exec_result.agent,
                metadata={
                    "success": str(exec_result.success),
                    "duration": str(duration),
                    "routing_confidence": str(routing_decision.get("confidence", 0)),
                },
            )
        except Exception:
            pass

        # ── Step 6: Log + auto-evaluate ──
        log_task(
            task_id=task_id,
            prompt=user_input,
            routing_decision=routing_decision,
            agent=exec_result.agent,
            result=exec_result.output,
            success=exec_result.success,
            duration=duration,
        )

        try:
            await evaluate_result(task_id, user_input, exec_result)
        except Exception:
            pass

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

    async def _execute_with_fallback(self, agent_id, prompt, adapters, routing):
        """Execute on primary agent, fall back if it fails."""
        if agent_id in adapters:
            result = await adapters[agent_id].execute(prompt)
            if result.success:
                return result

        # Try fallbacks
        for fb_id in get_fallbacks(agent_id):
            if fb_id in adapters:
                fb_adapter = adapters[fb_id]
                if fb_adapter.get_status().installed:
                    result = await fb_adapter.execute(prompt)
                    if result.success:
                        result.fallback_from = agent_id
                        return result

        # All failed — return last error
        if agent_id in adapters:
            return await adapters[agent_id].execute(prompt)

        # No adapter at all
        from friday.adapters.base import ExecResult
        return ExecResult(
            success=False, output="", duration=0,
            agent=agent_id, agent_name=agent_id,
            error="Agent not available",
        )

    async def get_status(self) -> dict:
        """Get full system status."""
        if not self._initialized:
            await self.initialize()

        model_status = await model_router.get_provider_status()
        health_results = await self.monitor.check_all()

        return {
            "models": model_status,
            "agents": health_results,
            "memory": {
                "short_term": len(short_term.get_recent(100)),
                "long_term": long_term.count(),
            },
        }
