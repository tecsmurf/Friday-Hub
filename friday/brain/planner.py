"""
Task Planner — Decomposes complex tasks into actionable sub-steps.

Decides whether a task needs a single agent or multi-step collaboration.
Uses the model router for LLM-based planning when the task is ambiguous.
"""

import json
from friday.models import router as model_router

_PLAN_PROMPT = """You are a task planner for an AI agent orchestration system.
Analyze the following task and decompose it into concrete sub-steps.

Task: "{task}"

Respond with ONLY a JSON object (no markdown, no explanation):
{{
  "complexity": "<simple|moderate|complex>",
  "needs_multiple_agents": <true|false>,
  "steps": [
    {{
      "step": 1,
      "description": "<what to do>",
      "agent_hint": "<claude-code|codex|openclaw|any>"
    }}
  ],
  "reasoning": "<why this decomposition>"
}}

Rules:
- "simple" = single command, 1 agent, 1 step
- "moderate" = clear goal, may need 2-3 steps, 1 agent can handle
- "complex" = vague/multi-part, needs decomposition, possibly multiple agents
- Most tasks are "simple" — only escalate when genuinely needed"""


async def plan(task: str, memory_context: str | None = None) -> dict:
    """
    Analyze a task and produce a plan.

    Returns:
        {
            "complexity": "simple" | "moderate" | "complex",
            "needs_multiple_agents": bool,
            "steps": [ { "step": int, "description": str, "agent_hint": str } ],
            "reasoning": str,
        }
    """
    # Quick heuristic — short tasks are almost always simple
    word_count = len(task.split())
    if word_count <= 10:
        return {
            "complexity": "simple",
            "needs_multiple_agents": False,
            "steps": [{"step": 1, "description": task, "agent_hint": "any"}],
            "reasoning": "Short task, single-step execution",
        }

    # For longer / ambiguous tasks, ask the LLM
    prompt = _PLAN_PROMPT.format(task=task)
    if memory_context:
        prompt += f"\n\nRelevant past experience:\n{memory_context}"

    try:
        response = await model_router.chat(
            messages=[{"role": "user", "content": prompt}],
            role="fast",
            json_mode=True,
        )

        content = response["content"].strip()
        # Handle markdown-wrapped JSON
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        plan_data = json.loads(content)

        # Validate and normalize
        complexity = plan_data.get("complexity", "simple")
        if complexity not in ("simple", "moderate", "complex"):
            complexity = "simple"

        steps = plan_data.get("steps", [])
        if not steps:
            steps = [{"step": 1, "description": task, "agent_hint": "any"}]

        return {
            "complexity": complexity,
            "needs_multiple_agents": plan_data.get("needs_multiple_agents", False),
            "steps": steps,
            "reasoning": plan_data.get("reasoning", "LLM-planned"),
        }
    except Exception:
        # Fallback — treat everything as simple
        return {
            "complexity": "simple",
            "needs_multiple_agents": False,
            "steps": [{"step": 1, "description": task, "agent_hint": "any"}],
            "reasoning": "Planning failed, defaulting to simple execution",
        }
