"""
Model Router — Dynamic model switching layer.

Tries Ollama (local, free) first. If Ollama is down or the model
isn't available, falls back to OpenRouter (cloud).

This is the ONLY module that other code calls for LLM inference.
Everything goes through here, making model switching transparent.
"""

from friday.models import ollama_client, openrouter_client
from friday.config import config

_models_cfg = config.get("models", {})


async def get_provider_status() -> dict:
    """Check which providers are available."""
    ollama_ok = await ollama_client.is_available()
    openrouter_ok = await openrouter_client.is_available()
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

    # --- Try primary (Ollama) ---
    if primary == "ollama":
        try:
            if await ollama_client.is_available():
                model = role_cfg.get("ollama", "qwen3.6:latest")
                return await ollama_client.chat(
                    model=model,
                    messages=messages,
                    temperature=temp,
                    max_tokens=max_tok,
                    json_mode=json_mode,
                )
        except Exception:
            pass  # Fall through to fallback

    # --- Try fallback (OpenRouter) ---
    if fallback == "openrouter":
        try:
            if await openrouter_client.is_available():
                model = role_cfg.get("openrouter", "qwen/qwen-2.5-72b-instruct")
                return await openrouter_client.chat(
                    model=model,
                    messages=messages,
                    temperature=temp,
                    max_tokens=max_tok,
                    json_mode=json_mode,
                )
        except Exception:
            pass

    # --- Both failed ---
    raise RuntimeError(
        "No LLM providers available. "
        "Start Ollama (`ollama serve`) or set OPENROUTER_API_KEY in .env"
    )


async def quick_prompt(prompt: str, role: str = "fast", json_mode: bool = False) -> str:
    """One-shot prompt helper. Returns just the content string."""
    result = await chat(
        messages=[{"role": "user", "content": prompt}],
        role=role,
        json_mode=json_mode,
    )
    return result["content"]
