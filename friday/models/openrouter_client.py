"""
OpenRouter Client — Cloud LLM fallback via OpenRouter API.

Used ONLY when Ollama is unavailable. Requires an API key.
Supports hundreds of models including Qwen, Llama, Mistral, etc.
"""

import os

import httpx

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_TIMEOUT = 60.0


def _get_key() -> str | None:
    return (os.environ.get("OPENROUTER_API_KEY") or "").strip() or None


def is_configured() -> bool:
    """True when an API key is set (no network call)."""
    return _get_key() is not None


async def is_available() -> bool:
    """Check that the configured API key is accepted by OpenRouter.

    Uses the authenticated ``/key`` endpoint; ``/models`` is public and would
    report any non-empty key as valid.
    """
    key = _get_key()
    if not key:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{OPENROUTER_BASE}/key",
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
        if r.status_code >= 400:
            try:
                err = r.json().get("error") or {}
                detail = err.get("message") if isinstance(err, dict) else str(err)
            except ValueError:
                detail = None
            raise RuntimeError(f"OpenRouter error {r.status_code} for model '{model}': {detail or r.reason_phrase}")
        data = r.json()

    choices = data.get("choices") or [{}]
    content = (choices[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    tokens = usage.get("total_tokens", 0)

    return {
        "content": content,
        "model": model,
        "provider": "openrouter",
        "tokens": tokens,
    }
