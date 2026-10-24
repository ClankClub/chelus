"""DeepSeek chat-completions client using only the standard library.

No SDK on purpose: the openai package pulls in Rust-built wheels (pydantic-core, jiter)
that do not build for 32-bit Termux (arm-unknown-linux-androideabi).
TLS verification uses Python's defaults and is never disabled.
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any

from .config import Settings


class ProviderError(Exception):
    """A failed model request, with a message safe to show the user."""


def _api_message(msg: dict[str, Any], thinking: bool) -> dict[str, Any]:
    """Shape a stored message for the API.

    DeepSeek rules (docs, plus lessons recorded in Hermes Agent):
    - thinking on: every assistant message must carry reasoning_content, non-empty (" " pad),
      or replayed tool turns get HTTP 400.
    - thinking off: reasoning_content is dropped.
    - an empty-string content on an assistant tool-call message is rejected; send null instead.
    """
    out = {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls", "tool_call_id", "name")}
    if msg.get("role") == "assistant":
        if out.get("tool_calls") and not out.get("content"):
            out["content"] = None
        if thinking:
            out["reasoning_content"] = msg.get("reasoning_content") or " "
    return out


def build_payload(s: Settings, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": s.model,
        "messages": [_api_message(m, s.thinking) for m in messages],
        "max_tokens": s.max_tokens,
        "thinking": {"type": "enabled" if s.thinking else "disabled"},
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    return payload


def chat(s: Settings, api_key: str, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
    """One non-streaming request. Returns the assistant message as a plain dict."""
    body = json.dumps(build_payload(s, messages, tools)).encode("utf-8")
    req = urllib.request.Request(
        s.base_url.rstrip("/") + "/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=s.request_timeout, context=ssl.create_default_context()) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            text = _http_error_text(e)
        finally:
            e.close()
        raise ProviderError(text) from None
    except (TimeoutError, socket.timeout):
        raise ProviderError(f"model request timed out after {s.request_timeout:g}s") from None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise ProviderError(f"model request timed out after {s.request_timeout:g}s") from None
        raise ProviderError(f"could not reach {s.base_url}: {reason}") from None
    except OSError as e:
        raise ProviderError(f"network error talking to {s.base_url}: {e}") from None

    try:
        data = json.loads(raw)
        choice = data["choices"][0]
        message = choice["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ProviderError(f"unexpected response from model API: {raw[:300]!r}") from None

    out: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
    if message.get("tool_calls"):
        out["tool_calls"] = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["function"]["name"], "arguments": tc["function"].get("arguments") or "{}"},
            }
            for tc in message["tool_calls"]
        ]
    if message.get("reasoning_content"):
        out["reasoning_content"] = message["reasoning_content"]
    out["_finish_reason"] = choice.get("finish_reason")
    out["_usage"] = data.get("usage")
    return out


def _http_error_text(e: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        body = e.read()[:2000]
        parsed = json.loads(body)
        detail = parsed.get("error", {}).get("message") or ""
    except Exception:
        pass
    hints = {
        401: "the API key was rejected (check ~/.config/chelys/deepseek.key)",
        402: "the DeepSeek account is out of balance",
        429: "rate limited by DeepSeek; wait and retry",
    }
    text = f"model API returned HTTP {e.code}"
    if e.code in hints:
        text += f": {hints[e.code]}"
    elif e.code >= 500:
        text += ": DeepSeek server error; retry later"
    if detail:
        text += f" ({detail[:300]})"
    return text
