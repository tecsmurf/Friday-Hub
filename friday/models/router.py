"""
Model Router — Dynamic model switching layer.

Tries Ollama (local, free) first. If Ollama is down or the request fails
(e.g. model not pulled), falls back to OpenRouter (cloud). When every
provider fails, the raised error lists each provider's reason.

This is the ONLY module that other code calls for LLM inference.
Everything goes through here, making model switching transparent.
"""

import asyncio
import logging

from friday.config import config
from friday.models import ollama_client, openrouter_client

log = logging.getLogger(__name__)

_models_cfg = config.get("models", {})


async def get_provider_status() -> dict:
    """Check which providers are available."""
    ollama_ok, openrouter_ok = await asyncio.gather(
        ollama_client.is_available(), openrouter_client.is_available()
    )
    return {
        "ollama": {"available": ollama_ok, "type": "local"},
        "openrouter": {"available": openrouter_ok, "type": "cloud"},
    }


async def chat(
    messages: list[dict],
    role: str = "brain",
    temperature: float | None = None,
    max_tokens: int | None = None,
    json_mode: bool = False,
) -> dict:
    """
    Send a chat request, automatically routing to the best provider.

    Args:
        messages: Chat messages [{"role": "...", "content": "..."}]
        role: "brain" (planning/routing) or "fast" (quick tasks)
        temperature: Override config temperature
        max_tokens: Override config max_tokens
        json_mode: Request JSON output

    Returns:
        { "content": str, "model": str, "provider": str, "tokens": int }
    """
    role_cfg = _models_cfg.get(role, _models_cfg.get("brain", {}))
    temp = temperature if temperature is not None else role_cfg.get("temperature", 0.3)
    max_tok = max_tokens if max_tokens is not None else role_cfg.get("max_tokens", 4096)

    primary = _models_cfg.get("primary_provider", "ollama")
    fallback = _models_cfg.get("fallback_provider", "openrouter")

    errors: list[str] = []
    for provider in dict.fromkeys(p for p in (primary, fallback) if p):
        if provider == "ollama":
            if not await ollama_client.is_available():
                errors.append(f"ollama: not reachable at {ollama_client.get_host()}")
                continue
            model = role_cfg.get("ollama", "qwen3:4b")
            client = ollama_client
        elif provider == "openrouter":
            if not openrouter_client.is_configured():
                errors.append("openrouter: OPENROUTER_API_KEY not set")
                continue
            model = role_cfg.get("openrouter", "qwen/qwen-2.5-72b-instruct")
            client = openrouter_client
        else:
            errors.append(f"{provider}: unknown provider")
            continue

        try:
            return await client.chat(
                model=model,
                messages=messages,
                temperature=temp,
                max_tokens=max_tok,
                json_mode=json_mode,
            )
        except Exception as exc:
            log.debug("%s chat failed: %s", provider, exc)
            errors.append(f"{provider}: {exc}")

    raise RuntimeError(
        "No LLM providers available. "
        "Start Ollama (`ollama serve`) or set OPENROUTER_API_KEY in .env"
        + (f" ({'; '.join(errors)})" if errors else "")
    )


async def quick_prompt(prompt: str, role: str = "fast", json_mode: bool = False) -> str:
    """One-shot prompt helper. Returns just the content string."""
    result = await chat(
        messages=[{"role": "user", "content": prompt}],
        role=role,
        json_mode=json_mode,
    )
    return result["content"]
