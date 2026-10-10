"""
AI Router — LLM-based dynamic task routing.

Uses Qwen (via Ollama) to intelligently decide which agent
should handle each task. Replaces static keyword matching.

The router is an EXECUTION HELPER — Hermes (the brain) is the
DECISION MAKER. The router just picks the best tool.
"""

import logging

from friday.config import config
from friday.models import router as model_router
from friday.models.parsing import parse_json_object

log = logging.getLogger(__name__)

_router_cfg = config.get("router", {})
_DEFAULT_AGENT = _router_cfg.get("default_agent", "claude-code")
_FALLBACKS = _router_cfg.get("fallbacks", {})

_ROUTING_PROMPT = """You are an AI task router for a multi-agent system.
You must decide which coding agent should handle the given task.

Available agents:
{agents_info}

User's task: "{task}"

Respond with ONLY a JSON object (no markdown, no explanation):
{{
  "agent": "<agent-id>",
  "reason": "<brief reason for selection>",
  "confidence": <0.0-1.0>
}}

Rules:
- Pick the agent whose capabilities best match the task
- If unsure, pick the default: {default}
- confidence < 0.5 means you're guessing"""


def pick_default(available_agents: dict) -> str:
    """The configured default agent if available, else the best-priority one."""
    if _DEFAULT_AGENT in available_agents or not available_agents:
        return _DEFAULT_AGENT
    return min(
        available_agents,
        key=lambda aid: (getattr(available_agents[aid], "priority", 99), aid),
    )


def _clamp_confidence(value) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


async def route(
    task: str,
    available_agents: dict,
    memory_context: str | None = None,
) -> dict:
    """
    Use LLM to decide which agent handles this task.

    Args:
        task: The user's task description
        available_agents: { agent_id: AdapterStatus } — only usable agents
        memory_context: Optional context from past similar tasks

    Returns:
        { "agent": str, "reason": str, "confidence": float }
        The agent is always one of ``available_agents`` when any exist.
    """
    default = pick_default(available_agents)

    agents_info = "\n".join(
        f"- {aid} ({status.name}): {', '.join(status.capabilities)}"
        for aid, status in available_agents.items()
    )
    if not agents_info:
        return {"agent": default, "reason": "No agents available", "confidence": 0.0}

    if len(available_agents) == 1:
        return {"agent": default, "reason": "Only available agent", "confidence": 1.0}

    prompt = _ROUTING_PROMPT.format(agents_info=agents_info, task=task, default=default)
    if memory_context:
        prompt += f"\n\nRelevant past experience (task → agent, outcome):\n{memory_context}"

    try:
        response = await model_router.chat(
            messages=[{"role": "user", "content": prompt}],
            role="fast",
            json_mode=True,
        )
        decision = parse_json_object(response["content"])
    except Exception as exc:
        log.info("AI routing failed, using %s: %s", default, exc)
        return {
            "agent": default,
            "reason": f"AI routing unavailable ({exc}); using {default}",
            "confidence": 0.3,
            "routing_failed": True,
        }

    agent = decision.get("agent")
    reason = decision.get("reason") or "AI-selected"
    confidence = _clamp_confidence(decision.get("confidence", 0.5))
    if agent not in available_agents:
        reason = f"Model chose unavailable agent '{agent}'; using {default}"
        agent = default
        confidence = min(confidence, 0.3)

    return {
        "agent": agent,
        "reason": str(reason),
        "confidence": confidence,
        "model_used": response.get("model", "unknown"),
        "provider": response.get("provider", "unknown"),
    }


def get_fallbacks(agent_id: str) -> list[str]:
    """Get fallback chain for an agent."""
    return list(_FALLBACKS.get(agent_id, []))
