# Contributing to Friday Hub

Thanks for wanting to improve Friday Hub! This guide covers everything you need
to get started.

## Development Setup

```bash
git clone https://github.com/<your-username>/friday-hub.git
cd friday-hub
python -m venv .venv
```

**Windows:**
```bash
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env
```

**macOS / Linux:**
```bash
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

## Verify Installation

```bash
friday status
friday config
```

If Ollama is installed, pull the default local models:

```bash
ollama pull qwen3:4b
ollama pull qwen2.5-coder:3b
```

## Running Tests

Tests are designed to run **offline** — no Ollama, no agent CLIs, no API keys:

```bash
pytest tests/ -v
```

For specific test modules:

```bash
pytest tests/test_config.py -v
pytest tests/test_short_term.py -v
pytest tests/test_adapters.py -v
pytest tests/test_feedback.py -v
pytest tests/test_context.py -v
```

## Architecture Overview

```
User → CLI → Hermes Brain → AI Router → Agent Adapter → Result
                ↕                           ↕
          Memory System              Health Monitor
       (short + long term)          (version guard)
```

**Key principles:**

- **Hermes is the brain**: All tasks flow through `friday/brain/hermes.py`
- **Adapters are thin**: Put shared logic in `BaseAdapter`, not concrete adapters
- **Router is a helper**: It picks the best tool; Hermes makes the decisions
- **Models go through the router**: Never call `ollama_client` or `openrouter_client` directly — use `friday.models.router`
- **ChromaDB is lazy**: Only initialized on first access to keep startup fast
- **Config is immutable at runtime**: Loaded once when `friday.config` is imported

## Code Style

- **Docstrings**: Every public module, class, and function needs a docstring
- **Type hints**: Use Python 3.10+ syntax (`str | None`, `list[str]`)
- **Async**: All I/O-bound operations should be async. The CLI wraps with `asyncio.run()`
- **Error handling**: Wrap external calls in try/except — never crash the orchestrator
- **Imports**: Group as stdlib → third-party → local, separated by blank lines

## Adding a New Agent

1. Create `friday/adapters/your_agent.py`:
   ```python
   from friday.adapters.base import BaseAdapter

   class YourAgentAdapter(BaseAdapter):
       def __init__(self, cfg: dict):
           super().__init__("your-agent", cfg)
   ```

2. Add the config to `friday.yaml`:
   ```yaml
   agents:
     your-agent:
       name: Your Agent
       command: your-agent-cmd
       enabled: true
       priority: 4
       capabilities: [your-capability]
       execute_args: ["{prompt}"]
       version_args: ["--version"]
   ```

3. Register it in `friday/health/monitor.py`:
   ```python
   from friday.adapters.your_agent import YourAgentAdapter
   # Add to adapter_map in _init_adapters()
   ```

4. Add fallback chains in `friday.yaml` → `router.fallbacks`

## Pull Requests

- Keep changes focused and easy to review
- Do **not** commit `.env`, ChromaDB files, task logs, virtual environments, or generated caches
- Update `README.md` when user-facing commands or behavior changes
- Add tests for new functionality
- Prefer adapter-level fixes when a third-party CLI changes
- Run `pytest -q` and `ruff check friday tests` before submitting

## Project Structure Reference

| Directory | Purpose |
|-----------|---------|
| `friday/brain/` | Hermes orchestrator + task planner |
| `friday/router/` | LLM-based agent routing |
| `friday/adapters/` | Agent CLI wrappers |
| `friday/memory/` | Short-term + long-term + logger |
| `friday/models/` | Ollama & OpenRouter clients + router |
| `friday/health/` | Health monitor + version guard |
| `friday/learning/` | Feedback & self-improvement |
| `friday/context/` | Project context gathering |
| `tests/` | Offline-safe unit tests |
