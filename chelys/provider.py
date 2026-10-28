"""DeepSeek chat-completions client using only the standard library.

No SDK on purpose: the openai package pulls in Rust-built wheels (pydantic-core, jiter)
that do not build for 32-bit Termux (arm-unknown-linux-androideabi).
TLS verification uses Python's defaults and is never disabled.
"""

from __future__ import annotations

import datetime
import email.utils
import json
import random
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .config import Settings


class ProviderError(Exception):
    """A failed model request, with a message safe to show the user.

    retryable: worth retrying now (429, 5xx, timeout, network, garbled 200).
    transient: the provider is only temporarily away (5xx, timeout, network, garbled 200), so
      a longer wait may still work. 429 is retryable but not transient, as in Hermes's
      auto-recovery ladder: a rate limit is a signal to slow down, not an outage.
    retry_after: seconds the server asked us to wait, if it said.
    """

    def __init__(self, message: str, *, retryable: bool = False, transient: bool = False,
                 retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.transient = transient
        self.retry_after = retry_after


# Retry policy, same numbers as Hermes Agent (retry_utils.py, turn_recovery.compute_error_backoff):
# a server-requested wait wins (capped at 600 s), otherwise min(2 * 2^(n-1), 60) s plus 0-50% jitter.
RETRY_AFTER_CAP_S = 600.0
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 60.0
LONG_WAIT_S = 60.0  # waits at least this long are announced to the user as they start
RESPONSE_MAX_BYTES = 2 * 1024 * 1024

# 408 request timeout, 429 rate limit, and every 5xx are transient.
RETRYABLE_STATUS = frozenset({408, 429})

OnRetry = Callable[[int, int, float, str], None]  # (attempt, max_retries, wait_s, error)


def jittered_backoff(attempt: int, base: float = BACKOFF_BASE_S, cap: float = BACKOFF_MAX_S,
                     jitter: float = 0.5) -> float:
    """min(base * 2^(attempt-1), cap) plus 0 to jitter*100% random jitter. attempt is 1-based."""
    delay = min(base * 2 ** max(0, attempt - 1), cap)
    return delay + random.uniform(0, jitter * delay)


def parse_retry_after(value: Any) -> float | None:
    """Seconds from a Retry-After value: a number, or an HTTP date. None if absent or junk."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds())


def _api_message(msg: dict[str, Any], thinking: bool) -> dict[str, Any]:
    """Shape a stored message for the API.

    A user message's api_content (the exact bytes first sent, including the turn's
    timestamp) is replayed instead of its clean content, so the prefix stays cacheable.

    DeepSeek rules (docs, plus lessons recorded in Hermes Agent):
    - thinking on: every assistant message must carry reasoning_content, non-empty (" " pad),
      or replayed tool turns get HTTP 400.
    - thinking off: reasoning_content is dropped.
    - an empty-string content on an assistant tool-call message is rejected; send null instead.
    """
    out = {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls", "tool_call_id", "name")}
    if msg.get("role") == "user" and msg.get("api_content"):
        out["content"] = msg["api_content"]
    if msg.get("role") == "assistant":
        if out.get("tool_calls") and not out.get("content"):
            out["content"] = None
        if thinking:
            out["reasoning_content"] = msg.get("reasoning_content") or " "
    return out


def build_payload(
    s: Settings, messages: list[dict], tools: list[dict], max_tokens: int | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": s.model,
        "messages": [_api_message(m, s.thinking) for m in messages],
        "max_tokens": max_tokens or s.max_tokens,
        "thinking": {"type": "enabled" if s.thinking else "disabled"},
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    return payload


def chat(
    s: Settings,
    api_key: str,
    messages: list[dict],
    tools: list[dict],
    *,
    max_tokens: int | None = None,
    on_retry: OnRetry | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """One model call, retried on transient failures. Returns the assistant message as a dict.

    Retries 429, 5xx, timeouts, network errors and garbled 200s up to s.api_max_retries times.
    Never retries 400/401/402/403/404: the same request would fail the same way.
    The wait is a plain sleep, so Ctrl-C still cancels it.
    """
    attempt = 0
    while True:
        try:
            return _chat_once(s, api_key, messages, tools, max_tokens)
        except ProviderError as e:
            if not e.retryable or attempt >= s.api_max_retries:
                if attempt:
                    raise ProviderError(f"{e} (gave up after {attempt} retries)", retryable=e.retryable,
                                        transient=e.transient, retry_after=e.retry_after) from None
                raise
            attempt += 1
            if e.retry_after is not None:
                wait = min(e.retry_after, RETRY_AFTER_CAP_S)
            else:
                wait = jittered_backoff(attempt)
            if on_retry is not None:
                on_retry(attempt, s.api_max_retries, wait, str(e))
            sleep(wait)


def _chat_once(
    s: Settings, api_key: str, messages: list[dict], tools: list[dict], max_tokens: int | None
) -> dict[str, Any]:
    body = json.dumps(build_payload(s, messages, tools, max_tokens)).encode("utf-8")
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
    timed_out = f"model request timed out after {s.request_timeout:g}s"
    deadline = time.monotonic() + s.request_deadline
    try:
        with urllib.request.urlopen(req, timeout=s.request_timeout, context=ssl.create_default_context()) as resp:
            raw = _read_until(resp, deadline, s.request_deadline)
    except urllib.error.HTTPError as e:
        try:
            err = _http_error(e)
        finally:
            e.close()
        raise err from None
    except (TimeoutError, socket.timeout):
        raise ProviderError(timed_out, retryable=True, transient=True) from None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise ProviderError(timed_out, retryable=True, transient=True) from None
        if isinstance(reason, ssl.SSLCertVerificationError):
            # Deterministic (Hermes's error_classifier fails fast on these): retrying can't help.
            raise ProviderError(f"TLS certificate check failed for {s.base_url}: {getattr(reason, 'verify_message', None) or reason}. "
                                "Check the phone's date and time.") from None
        raise ProviderError(f"could not reach {s.base_url}: {reason}", retryable=True, transient=True) from None
    except ssl.SSLCertVerificationError as e:
        raise ProviderError(f"TLS certificate check failed for {s.base_url}: {getattr(e, 'verify_message', None) or e}. "
                            "Check the phone's date and time.") from None
    except OSError as e:
        raise ProviderError(f"network error talking to {s.base_url}: {e}", retryable=True, transient=True) from None

    try:
        data = json.loads(raw)
        choice = data["choices"][0]
        message = choice["message"]
        if not isinstance(message, dict):
            raise ValueError("message must be an object")
        for field in ("content", "reasoning_content"):
            if message.get(field) is not None and not isinstance(message[field], str):
                raise ValueError(f"{field} must be text or null")
        calls = message.get("tool_calls")
        if calls is not None:
            if not isinstance(calls, list):
                raise ValueError("tool_calls must be a list")
            for tc in calls:
                if not isinstance(tc, dict) or not isinstance(tc.get("function"), dict):
                    raise ValueError("invalid tool call")
                fn = tc["function"]
                if (not isinstance(tc.get("id"), str) or not tc["id"].strip()
                        or not isinstance(fn.get("name"), str) or not fn["name"].strip()
                        or (fn.get("arguments") is not None and not isinstance(fn["arguments"], str))):
                    raise ValueError("invalid tool call fields")
    except (ValueError, KeyError, IndexError, TypeError, RecursionError):
        # Hermes treats a 200 without choices like a transient server error ("empty
        # responses often mean rate limiting"), so it is retried.
        raise ProviderError(f"unexpected response from model API: {raw[:300]!r}", retryable=True, transient=True) from None

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


def _read_until(resp: Any, deadline: float, total: float) -> bytes:
    """Read the body, but give up once the whole request has taken longer than `total`.

    urllib's timeout covers each socket read, not the request, so a server trickling a
    byte at a time could hold the single-threaded bridge forever. read1 returns whatever
    has arrived, so the deadline is checked between chunks; a check can be late by at most
    one socket timeout.
    """
    chunks = []
    size = 0
    while True:
        if time.monotonic() > deadline:
            raise ProviderError(f"model request took longer than {total:g}s in total",
                                retryable=True, transient=True)
        chunk = resp.read1(65536)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > RESPONSE_MAX_BYTES:
            raise ProviderError(f"model response exceeded {RESPONSE_MAX_BYTES} bytes",
                                retryable=True, transient=True)
        chunks.append(chunk)


def _http_error(e: urllib.error.HTTPError) -> ProviderError:
    detail = ""
    retry_after = parse_retry_after(e.headers.get("Retry-After") if e.headers else None)
    try:
        parsed = json.loads(e.read(2000))
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                detail = err.get("message") or ""
                if retry_after is None:
                    retry_after = parse_retry_after(err.get("retry_after"))
            if retry_after is None:
                retry_after = parse_retry_after(parsed.get("retry_after"))
    except Exception:
        pass
    hints = {
        401: "the API key was rejected (check ~/.config/chelys/deepseek.key)",
        402: "the DeepSeek account is out of balance",
        429: "rate limited by DeepSeek",
    }
    text = f"model API returned HTTP {e.code}"
    if e.code in hints:
        text += f": {hints[e.code]}"
    elif e.code >= 500:
        text += ": DeepSeek server error"
    if detail:
        text += f" ({str(detail)[:300]})"
    return ProviderError(text, retryable=e.code in RETRYABLE_STATUS or e.code >= 500,
                         transient=e.code == 408 or e.code >= 500, retry_after=retry_after)
