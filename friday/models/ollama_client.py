"""
Ollama Client — Local LLM inference via Ollama REST API.

Talks to Ollama running on localhost:11434 (or ``OLLAMA_HOST``).
This is the PRIMARY provider — free, local, no API keys needed.
"""

import os

import httpx

DEFAULT_HOST = "http://localhost:11434"
_TIMEOUT = 120.0  # 2 min for generation


def get_host() -> str:
    """Resolve ``OLLAMA_HOST`` at call time, using Ollama's own conventions.

    Read lazily so values loaded from ``.env`` by :mod:`friday.config` apply
    regardless of import order. Accepts ``host``, ``host:port`` or a full URL;
    the bind-all address ``0.0.0.0`` is mapped to ``localhost``.
    """
    raw = (os.environ.get("OLLAMA_HOST") or "").strip().rstrip("/")
    if not raw:
        return DEFAULT_HOST
    if "://" not in raw:
        raw = f"http://{raw}"
    scheme, rest = raw.split("://", 1)
    host_port, _, path = rest.partition("/")
    if host_port.startswith("["):
        has_port = "]:" in host_port
    else:
        has_port = ":" in host_port
    if not has_port:
        host_port = f"{host_port}:11434"
    if host_port.startswith("0.0.0.0:"):
        host_port = "localhost" + host_port[len("0.0.0.0"):]
    return f"{scheme}://{host_port}" + (f"/{path}" if path else "")


async def is_available() -> bool:
    """Check if Ollama server is running."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{get_host()}/api/tags")
            return r.status_code == 200
    except Exception:
        return False


async def list_models() -> list[str]:
    """List locally available models."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{get_host()}/api/tags")
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

    Raises:
        RuntimeError: with Ollama's own error text (e.g. model not found).
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
        r = await client.post(f"{get_host()}/api/chat", json=payload)
        if r.status_code >= 400:
            try:
                detail = r.json().get("error") or r.text
            except ValueError:
                detail = r.text
            raise RuntimeError(f"Ollama error {r.status_code} for model '{model}': {detail}")
        data = r.json()

    content = data.get("message", {}).get("content", "")
    tokens = data.get("eval_count", 0) + data.get("prompt_eval_count", 0)

    return {
        "content": content,
        "model": model,
        "provider": "ollama",
        "tokens": tokens,
    }
