"""
Friday Hub — AI Orchestration Operating System.

A local-first terminal orchestrator that routes tasks to AI coding agents
(Claude Code, Codex CLI, OpenClaw) using LLM-powered intelligent routing,
with persistent memory, structured logging, and self-improving feedback.

Subpackages:
    adapters    Agent CLI wrappers (Claude Code, Codex, OpenClaw)
    brain       Hermes central orchestrator and task planner
    context     Project-aware context gathering
    health      Agent health monitoring and version guard
    learning    Feedback scoring and self-improvement
    memory      Short-term, long-term (ChromaDB), and JSON log storage
    models      LLM provider layer (Ollama, OpenRouter)
    router      LLM-based intelligent agent routing

Usage::

    # As a CLI (installed)
    friday run "refactor auth module"
    friday status
    friday doctor

    # As a library
    from friday.brain.hermes import HermesBrain

    brain = HermesBrain()
    await brain.initialize()
    result = await brain.think("debug the failing test")
"""

__version__ = "0.1.0"
