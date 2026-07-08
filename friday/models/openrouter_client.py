"""
OpenRouter Client — Cloud LLM fallback via OpenRouter API.

Used ONLY when Ollama is unavailable. Requires an API key.
Supports hundreds of models including Qwen, Llama, Mistral, etc.
"""

import httpx
import os

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_TIMEOUT = 60.0


def _get_key() -> str | None:
    return os.environ.get("OPENROUTER_API_KEY")


async def is_available() -> bool:
    """Check if OpenRouter API key is configured and valid."""
    key = _get_key()
    if not key:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{OPENROUTER_BASE}/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            return r.status_code == 200
    except Exception:
        return False


async def chat(
    model: str,
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 4096,
    json_mode: bool = False,
) -> dict:
    """
    Send a chat completion request to OpenRouter.
    
    Returns: { "content": str, "model": str, "provider": "openrouter", "tokens": int }
    """
    key = _get_key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set in .env")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://friday-hub.local",
        "X-Title": "Friday Hub",
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            f"{OPENROUTER_BASE}/chat/completions",
            json=payload,
            headers=headers,
        )
        r.raise_for_status()
        data = r.json()

    choice = data.get("choices", [{}])[0]
    content = choice.get("message", {}).get("content", "")
    usage = data.get("usage", {})
    tokens = usage.get("total_tokens", 0)

    return {
        "content": content,
        "model": model,
        "provider": "openrouter",
        "tokens": tokens,
    }
