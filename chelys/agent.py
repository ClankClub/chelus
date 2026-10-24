"""The agent loop: ask the model, run the tools it asks for, send results back, repeat
until it answers in plain text or a limit is hit.

Loop shape and limits follow Hermes Agent (Nous Research, MIT): an iteration cap per turn,
bounded tool output, and tool errors returned to the model as results instead of crashing.
"""

from __future__ import annotations

import json
import platform
import time
from importlib import resources
from typing import Any, Callable

from . import provider, tools
from .config import Settings
from .store import Store, recent_window

Approve = Callable[[str], bool]
Event = Callable[[str, str], None]


def load_persona(s: Settings) -> str:
    if s.persona_path.exists():
        return s.persona_path.read_text(encoding="utf-8").strip()
    return resources.files("chelys").joinpath("persona_default.md").read_text(encoding="utf-8").strip()


def system_prompt(s: Settings) -> str:
    risky = ", ".join(n for n, t in tools.TOOLS.items() if t.risky)
    return (
        f"{load_persona(s)}\n\n"
        "## Where you are\n"
        f"You run inside Termux on a small Android phone ({platform.machine()}, Python {platform.python_version()}). "
        f"Your tools act on this phone. Working directory: {s.workdir}. "
        f"Local time: {time.strftime('%Y-%m-%d %H:%M %Z')}.\n"
        f"The owner must approve {risky} before they run; a denial is final for that call, so don't retry it. "
        f"You get at most {s.max_iterations} model steps per message, and tool output is cut off after "
        f"{s.tool_output_chars} characters. Keep replies short: the owner may be reading on a phone."
    )


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [cut off: {len(text) - limit} more characters]"


class Agent:
    def __init__(
        self,
        s: Settings,
        api_key: str,
        store: Store,
        session_id: int,
        approve: Approve,
        on_event: Event | None = None,
        chat_fn: Callable[..., dict[str, Any]] = provider.chat,
    ):
        self.s = s
        self.api_key = api_key
        self.store = store
        self.session_id = session_id
        self.approve = approve
        self.on_event = on_event or (lambda kind, text: None)
        self.chat_fn = chat_fn

    def run_turn(self, user_text: str) -> str:
        """Run one user message to a final reply and save the turn.

        Ctrl-C or a model/API failure still saves the user message plus a short note,
        so the next turn knows what happened, without a half-finished tool exchange.
        """
        turn: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
        tools_run: list[str] = []
        try:
            reply = self._loop(turn, tools_run)
        except KeyboardInterrupt:
            self._save_aborted(user_text, "cancelled by the owner (Ctrl-C)", tools_run)
            raise
        except provider.ProviderError as e:
            self._save_aborted(user_text, f"failed: {e}", tools_run)
            raise
        self.store.append_turn(self.session_id, turn)
        return reply

    def _loop(self, turn: list[dict[str, Any]], tools_run: list[str]) -> str:
        history = self.store.messages(self.session_id)
        schemas = tools.schemas()
        for _ in range(self.s.max_iterations):
            window = recent_window(history + turn, self.s.history_chars)
            messages = [{"role": "system", "content": system_prompt(self.s)}] + window
            msg = self.chat_fn(self.s, self.api_key, messages, schemas)
            finish = msg.pop("_finish_reason", None)
            msg.pop("_usage", None)
            turn.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                text = msg.get("content") or "(no reply)"
                if finish == "length":
                    text += "\n[reply cut off: hit max_tokens]"
                    msg["content"] = text
                return text
            for call in calls:
                tools_run.append(call["function"]["name"])
                turn.append(self._run_tool(call))
        note = f"[stopped: hit the limit of {self.s.max_iterations} model steps for one message]"
        turn.append({"role": "assistant", "content": note})
        return note

    def _run_tool(self, call: dict[str, Any]) -> dict[str, Any]:
        name = call["function"]["name"]
        result = self._tool_result(name, call["function"].get("arguments") or "{}")
        return {"role": "tool", "tool_call_id": call["id"], "content": _clip(result, self.s.tool_output_chars)}

    def _tool_result(self, name: str, raw_args: str) -> str:
        tool = tools.TOOLS.get(name)
        if tool is None:
            return f"error: no tool named {name!r}"
        try:
            args = json.loads(raw_args)
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            return f"error: bad arguments for {name}: {e}"
        summary = tools.describe_call(name, args)
        if tool.risky and not self.approve(summary):
            self.on_event("denied", summary)
            return "denied: the owner did not approve this action"
        self.on_event("tool", summary)
        try:
            out = tool.handler(self.s, args)
        except tools.ToolError as e:
            out = f"error: {e}"
        except OSError as e:
            out = f"error: {e}"
        self.on_event("result", out.splitlines()[0][:120] if out else "(empty)")
        return out

    def _save_aborted(self, user_text: str, why: str, tools_run: list[str]) -> None:
        note = f"[this turn {why}"
        if tools_run:
            note += f"; tools that already ran: {', '.join(tools_run)}"
        note += "]"
        self.store.append_turn(
            self.session_id,
            [{"role": "user", "content": user_text}, {"role": "assistant", "content": note}],
        )
