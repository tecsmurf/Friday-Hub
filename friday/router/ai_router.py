"""
AI Router — LLM-based dynamic task routing.

Uses Qwen (via Ollama) to intelligently decide which agent
should handle each task. Replaces static keyword matching.

The router is an EXECUTION HELPER — Hermes (the brain) is the
DECISION MAKER. The router just picks the best tool.
"""

import json
from friday.models import router as model_router
from friday.config import config

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


async def route(
    task: str,
    available_agents: dict,
    memory_context: str | None = None,
) -> dict:
    """
    Use LLM to decide which agent handles this task.

    Args:
        task: The user's task description
        available_agents: { agent_id: AdapterStatus }
        memory_context: Optional context from past similar tasks

    Returns:
        { "agent": str, "reason": str, "confidence": float }
    """
    # Build agent info for the prompt
    agents_info = ""
    for aid, status in available_agents.items():
        if status.installed and status.enabled:
            caps = ", ".join(status.capabilities)
            agents_info += f"- {aid} ({status.name}): {caps}\n"

    if not agents_info.strip():
        return {"agent": _DEFAULT_AGENT, "reason": "No agents available", "confidence": 0.0}

    prompt = _ROUTING_PROMPT.format(
        agents_info=agents_info.strip(),
        task=task,
        default=_DEFAULT_AGENT,
    )

    if memory_context:
        prompt += f"\n\nRelevant past experience:\n{memory_context}"

    try:
        response = await model_router.chat(
            messages=[{"role": "user", "content": prompt}],
            role="fast",
            json_mode=True,
        )

        content = response["content"].strip()
        # Parse JSON from response (handle potential markdown wrapping)
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        decision = json.loads(content)

        # Validate agent exists and is available
        agent = decision.get("agent", _DEFAULT_AGENT)
        if agent not in available_agents:
            agent = _DEFAULT_AGENT

        return {
            "agent": agent,
            "reason": decision.get("reason", "AI-selected"),
            "confidence": min(1.0, max(0.0, float(decision.get("confidence", 0.5)))),
            "model_used": response.get("model", "unknown"),
            "provider": response.get("provider", "unknown"),
        }
    except Exception as e:
        # Fallback to priority-based routing
        return {
            "agent": _DEFAULT_AGENT,
            "reason": f"AI routing failed ({e}), using default",
            "confidence": 0.3,
        }


def get_fallbacks(agent_id: str) -> list[str]:
    """Get fallback chain for an agent."""
    return _FALLBACKS.get(agent_id, [])
