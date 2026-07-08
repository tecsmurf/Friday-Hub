"""Friday Hub — Model clients package.

LLM provider abstraction layer. All other code calls ``router.chat()``
for inference — never the individual clients directly. The router
transparently handles provider fallback (Ollama → OpenRouter).

Modules:
    router              Dynamic model switching (the public API)
    ollama_client       Local Ollama REST API client (primary)
    openrouter_client   Cloud OpenRouter API client (fallback)
"""
