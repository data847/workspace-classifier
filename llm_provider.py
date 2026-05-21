from __future__ import annotations

import json
import os
import threading
from typing import Any, Literal

import env_loader  # noqa: F401 — load `.env` before env reads below

LLMProvider = Literal["anthropic", "openai"]

# ---------------------------------------------------------------------------
# Singleton clients — created once, reused across all threads.
# Both Anthropic and OpenAI clients are thread-safe for concurrent requests.
# ---------------------------------------------------------------------------
_client_lock = threading.Lock()
_anthropic_client: Any = None
_openai_client: Any = None

_API_TIMEOUT = float(os.environ.get("LLM_REQUEST_TIMEOUT", "120"))


def _get_anthropic_client() -> Any:
    global _anthropic_client
    if _anthropic_client is None:
        with _client_lock:
            if _anthropic_client is None:
                import httpx
                from anthropic import Anthropic
                _anthropic_client = Anthropic(
                    timeout=httpx.Timeout(_API_TIMEOUT, connect=10.0)
                )
    return _anthropic_client


def _get_openai_client() -> Any:
    global _openai_client
    if _openai_client is None:
        with _client_lock:
            if _openai_client is None:
                from openai import OpenAI
                key = (
                    os.environ.get("OPENAI_API_KEY")
                    or os.environ.get("ANTHROPIC_API_KEY")
                    or ""
                ).strip()
                if not key:
                    raise RuntimeError(
                        "OpenAI API key not set. Set OPENAI_API_KEY, or use "
                        "ANTHROPIC_API_KEY when LLM_PROVIDER=openai."
                    )
                _openai_client = OpenAI(api_key=key, timeout=_API_TIMEOUT)
    return _openai_client


def llm_provider() -> LLMProvider:
    explicit = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if explicit == "openai":
        return "openai"
    if explicit == "anthropic":
        return "anthropic"

    if (os.environ.get("OPENAI_API_KEY") or "").strip():
        return "openai"

    akey = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if akey.startswith("sk-ant-"):
        return "anthropic"
    # OpenAI project / standard keys often use sk-proj-…
    if akey.startswith("sk-proj-") or akey.startswith("sk-or-v1-"):
        return "openai"

    return "anthropic"


def llm_api_configured() -> bool:
    if llm_provider() == "openai":
        return bool(
            (os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        )
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())


def default_llm_model() -> str:
    if llm_provider() == "openai":
        return (
            os.environ.get("OPENAI_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
            or "gpt-4o-mini"
        ).strip()
    return (os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-6").strip()


def _anthropic_tool_to_openai(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool["input_schema"],
        },
    }


def is_retryable_llm_error(exc: BaseException) -> bool:
    mod = type(exc).__module__
    name = type(exc).__name__
    if mod.startswith("anthropic"):
        return name in ("APIConnectionError", "APITimeoutError", "RateLimitError", "OverloadedError")
    if mod.startswith("openai"):
        if name in ("APIConnectionError", "APITimeoutError", "RateLimitError"):
            return True
        if name == "APIStatusError":
            code = getattr(exc, "status_code", None)
            return code in (408, 429, 500, 502, 503, 529)
    return False


def call_forced_tool(
    *,
    model: str,
    max_tokens: int,
    system: str,
    tools: list[dict[str, Any]],
    tool_name: str,
    user_content: str,
) -> dict[str, Any]:
    """One model turn with a single forced tool; returns parsed tool arguments (object)."""
    if llm_provider() == "anthropic":
        client = _get_anthropic_client()
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user_content}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                inp = getattr(block, "input", None)
                return inp if isinstance(inp, dict) else {}
        raise RuntimeError(
            f"Model did not return tool_use {tool_name!r}. "
            f"stop_reason={getattr(resp, 'stop_reason', None)!r}"
        )

    client = _get_openai_client()
    oai_tools = [_anthropic_tool_to_openai(t) for t in tools]
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        tools=oai_tools,
        tool_choice={"type": "function", "function": {"name": tool_name}},
    )
    choice = resp.choices[0]
    msg = choice.message
    tcalls = getattr(msg, "tool_calls", None) or []
    for tc in tcalls:
        fn = getattr(tc, "function", None)
        if fn and fn.name == tool_name:
            raw = fn.arguments or "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"OpenAI tool arguments were not valid JSON: {raw[:300]!r}") from e
            return parsed if isinstance(parsed, dict) else {}
    raise RuntimeError(
        f"OpenAI model did not return tool_calls for {tool_name!r}. "
        f"finish_reason={getattr(choice, 'finish_reason', None)!r}"
    )
