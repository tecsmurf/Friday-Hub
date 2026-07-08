"""
Ollama Client — Local LLM inference via Ollama REST API.

Talks to Ollama running on localhost:11434.
This is the PRIMARY provider — free, local, no API keys needed.
"""

import httpx
import os
import json

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
_TIMEOUT = 120.0  # 2 min for generation


async def is_available() -> bool:
    """Check if Ollama server is running."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{OLLAMA_HOST}/api/tags")
            return r.status_code == 200
    except Exception:
        return False


async def list_models() -> list[str]:
    """List locally available models."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{OLLAMA_HOST}/api/tags")
            if r.status_code == 200:
                data = r.json()
                return [m["name"] for m in data.get("models", [])]
    except Exception:
        pass
    return []


async def chat(
    model: str,
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 4096,
    json_mode: bool = False,
) -> dict:
    """
    Send a chat completion request to Ollama.
    
    Returns: { "content": str, "model": str, "provider": "ollama", "tokens": int }
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    if json_mode:
        payload["format"] = "json"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(f"{OLLAMA_HOST}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()

    content = data.get("message", {}).get("content", "")
    tokens = data.get("eval_count", 0) + data.get("prompt_eval_count", 0)

    return {
        "content": content,
        "model": model,
        "provider": "ollama",
        "tokens": tokens,
    }
