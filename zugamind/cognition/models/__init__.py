"""LLM clients — Ollama (local) and Claude (API).

Re-exports the public surface so callers can `from cognition.models import X`
instead of having to know which sub-module each function lives in.
"""
from .ollama import ollama_query, ollama_available
from .claude import query_claude_api

__all__ = [
    # ollama
    "ollama_query",
    "ollama_available",
    # claude
    "query_claude_api",
]
