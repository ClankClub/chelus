"""Per-turn tool-loop guard: notice when the model keeps making the same call without
progress, warn it, and (when hard stops are on) stop the turn.

Rebuilt from Hermes Agent's agent/tool_guardrails.py (Nous Research, MIT) with the same
detectors and thresholds, minus the parts for tools Chelys doesn't have (web search caps,
subagent caps, pollers, spill files):

- exact failure: the same call (tool + canonical args) failing again. Warn at 2, block at 5.
  A successful mutation in between makes the retry a new experiment and resets the count.
- same-tool failure: one tool failing with different args. Warn at 3, halt at 8, never
  halts a failure-tolerant tool (a run of different failing shell commands is diagnosis).
- read-only no progress: a read-only call returning the same result. Warn at 2, block at 5.
- identical streak: consecutive identical (tool, args, result) calls of any tool. A note at
  3, halt at 5. Repeating cycles of 2-4 calls (A,B,A,B...) are caught the same way.
- from the 2nd byte-identical repeat, a result of 512+ chars is replaced by a short stub
  (the tool still ran; only the copy in context shrinks).

Warnings never stop a call. Blocks and halts happen only with hard_stop=True, which Hermes
turns on for unattended surfaces (its gateway) and off for interactive ones (its CLI).
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

IDEMPOTENT_TOOLS = frozenset({"read_file", "list_dir", "battery_status"})
FAILURE_TOLERANT_TOOLS = frozenset({"run_shell"})
PROGRESS_TOOLS = frozenset({"write_file", "run_shell"})  # a success here is progress

EXACT_FAILURE_WARN, EXACT_FAILURE_BLOCK = 2, 5
SAME_TOOL_FAILURE_WARN, SAME_TOOL_FAILURE_HALT = 3, 8
NO_PROGRESS_WARN, NO_PROGRESS_BLOCK = 2, 5
IDENTICAL_NOTICE_AT = 3
MAX_CYCLE_PERIOD = 4
CYCLE_HISTORY = 64
STUB_MIN_CHARS = 512
STUB_ARGS_PREVIEW = 120

_MESSAGES = {
    "repeated_exact_failure_block": (
        "Blocked {tool}: the same tool call failed {count} times with identical arguments. "
        "Stop retrying it unchanged; change strategy or explain the blocker."),
    "idempotent_no_progress_block": (
        "Blocked {tool}: this read-only call returned the same result {count} times. "
        "Stop repeating it unchanged; use the result already provided or try a different query."),
    "same_tool_failure_halt": (
        "Stopped {tool}: it failed {count} times this turn. "
        "Stop retrying the same failing tool path and choose a different approach."),
    "repeated_exact_failure_warning": (
        "{tool} has failed {count} times with identical arguments. This looks like a loop; "
        "inspect the error and change strategy instead of retrying it unchanged."),
    "same_tool_failure_warning": (
        "{tool} has failed {count} times this turn. This looks like a loop. Inspect the latest "
        "error and check your assumptions before retrying: try different arguments, an absolute "
        "path, or a different tool. If the blocker is outside your control, say so instead of "
        "repeating the same failing path."),
    "idempotent_no_progress_warning": (
        "{tool} returned the same result {count} times. Use the result already provided "
        "or change the query instead of repeating it unchanged."),
    "identical_call_streak_halt": (
        "Stopped {tool}: the same call with identical arguments returned the same result "
        "{count} times in a row. Stop repeating it unchanged; use the result already provided or change strategy."),
    "identical_cycle_halt": (
        "Stopped {tool}: the same repeating cycle of tool calls (period {period}) with identical "
        "arguments and identical results has run {count} times. Repeating the batch unchanged is not "
        "progress; use the results already provided or change strategy."),
}

_STREAK_NOTICE = (
    "[note: this is the {ordinal} consecutive identical call to {tool} with identical arguments "
    "returning the same result. Do not repeat it; change arguments, use a different tool, or "
    "proceed with what you have.]")
_CYCLE_NOTICE = (
    "[note: the last {count} rounds repeated the same cycle of {period} tool calls (ending with "
    "{tool}) with identical arguments and identical results. Do not repeat the batch; change "
    "arguments, use a different tool, or proceed with what you have.]")


@dataclass(frozen=True)
class Decision:
    action: str = "allow"  # allow | warn | block | halt
    code: str = "allow"
    message: str = ""
    tool: str = ""
    count: int = 0

    @property
    def allows_execution(self) -> bool:
        return self.action in ("allow", "warn")

    @property
    def should_halt(self) -> bool:
        return self.action in ("block", "halt")

    def guidance(self) -> str:
        """Text appended to the tool result for a warn or halt."""
        if self.action not in ("warn", "halt") or not self.message:
            return ""
        label = "Tool loop hard stop" if self.action == "halt" else "Tool loop warning"
        return f"\n\n[{label}: {self.code}; count={self.count}; {self.message}]"


def canonical_args(args: Any) -> str:
    return json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


class Guard:
    """Tracks one turn's tool calls. Side-effect free: it returns decisions and text; the
    agent decides what to do with them."""

    def __init__(self, hard_stop: bool = False):
        self.hard_stop = hard_stop
        self.halt: Decision | None = None
        self._exact_failures: dict[tuple[str, str], int] = {}
        self._tool_failures: dict[str, int] = {}
        self._progress_since_failure: dict[tuple[str, str], bool] = {}
        self._no_progress: dict[tuple[str, str], tuple[str, int]] = {}
        self._streak: tuple[tuple[str, str], str] | None = None
        self._streak_count = 0
        self._streak_first_id = ""
        self._history: deque[tuple[tuple[str, str], str]] = deque(maxlen=CYCLE_HISTORY)

    def _decide(self, action: str, code: str, tool: str, count: int, **fmt: Any) -> Decision:
        d = Decision(action, code, _MESSAGES[code].format(tool=tool, count=count, **fmt), tool, count)
        if d.should_halt and self.halt is None:
            self.halt = d
        return d

    def before_call(self, tool: str, args: dict) -> Decision:
        if not self.hard_stop:
            return Decision(tool=tool)
        sig = (tool, _sha(canonical_args(args)))
        exact = 0 if self._progress_since_failure.get(sig) else self._exact_failures.get(sig, 0)
        if exact >= EXACT_FAILURE_BLOCK:
            return self._decide("block", "repeated_exact_failure_block", tool, exact)
        rec = self._no_progress.get(sig) if tool in IDEMPOTENT_TOOLS else None
        if rec is not None and rec[1] >= NO_PROGRESS_BLOCK:
            return self._decide("block", "idempotent_no_progress_block", tool, rec[1])
        return Decision(tool=tool)

    def after_call(self, tool: str, args: dict, result: str, failed: bool) -> Decision:
        sig = (tool, _sha(canonical_args(args)))
        if failed:
            if self._progress_since_failure.pop(sig, False):
                self._exact_failures.pop(sig, None)
            exact = self._exact_failures[sig] = self._exact_failures.get(sig, 0) + 1
            same = self._tool_failures[tool] = self._tool_failures.get(tool, 0) + 1
            self._no_progress.pop(sig, None)
            if self.hard_stop and tool not in FAILURE_TOLERANT_TOOLS and same >= SAME_TOOL_FAILURE_HALT:
                return self._decide("halt", "same_tool_failure_halt", tool, same)
            if exact >= EXACT_FAILURE_WARN:
                return self._decide("warn", "repeated_exact_failure_warning", tool, exact)
            if same >= SAME_TOOL_FAILURE_WARN:
                return self._decide("warn", "same_tool_failure_warning", tool, same)
            return Decision(tool=tool, count=exact)

        self._exact_failures.pop(sig, None)
        self._tool_failures.pop(tool, None)
        if tool in PROGRESS_TOOLS:
            self._progress_since_failure.update(dict.fromkeys(self._exact_failures, True))
            self._tool_failures.clear()
        if tool not in IDEMPOTENT_TOOLS:
            self._no_progress.pop(sig, None)
            return Decision(tool=tool)
        h = _sha(result)
        prev = self._no_progress.get(sig)
        repeats = prev[1] + 1 if prev is not None and prev[0] == h else 1
        self._no_progress[sig] = (h, repeats)
        if repeats >= NO_PROGRESS_WARN:
            return self._decide("warn", "idempotent_no_progress_warning", tool, repeats)
        return Decision(tool=tool, count=repeats)

    def observe_call(self, tool: str, args: dict, result: str, call_id: str, failed: bool) -> tuple[str, str]:
        """Return (notice, stub). The notice is appended to the result; the stub, when
        non-empty, replaces a byte-identical repeat result."""
        sig = (tool, _sha(canonical_args(args)))
        key = (sig, _sha(result))
        if key == self._streak:
            self._streak_count += 1
        else:
            self._streak, self._streak_count, self._streak_first_id = key, 1, call_id
        count = self._streak_count

        notice = ""
        if count >= IDENTICAL_NOTICE_AT:
            notice = _STREAK_NOTICE.format(ordinal=_ordinal(count), tool=tool)
            if self.hard_stop and count >= NO_PROGRESS_BLOCK and self.halt is None:
                self._decide("halt", "identical_call_streak_halt", tool, count)

        self._history.append(key)
        if not notice:
            cycle = self._cycle()
            if cycle is not None:
                period, laps = cycle
                notice = _CYCLE_NOTICE.format(count=laps, period=period, tool=tool)
                if self.hard_stop and laps >= NO_PROGRESS_BLOCK and self.halt is None:
                    self._decide("halt", "identical_cycle_halt", tool, laps, period=period)

        stub = ""
        if count >= 2 and not failed and len(result) >= STUB_MIN_CHARS:
            preview = canonical_args(args)
            if len(preview) > STUB_ARGS_PREVIEW:
                preview = preview[:STUB_ARGS_PREVIEW] + "..."
            ref = f" (tool_call_id {self._streak_first_id})" if self._streak_first_id else ""
            stub = (f"[note: this result is byte-identical to the {tool} result earlier this turn{ref}. "
                    f"Refer to that result; it has not changed. Args: {preview}]")
        return notice, stub

    def _cycle(self) -> tuple[int, int] | None:
        """(period, laps) for the smallest period 2..4 whose trailing laps all match the last
        lap, once laps reach the notice threshold. Period 1 is the streak's job."""
        h = self._history
        for period in range(2, MAX_CYCLE_PERIOD + 1):
            if len(h) < period * IDENTICAL_NOTICE_AT:
                continue
            laps = 1
            while True:
                base = len(h) - period * (laps + 1)
                if base < 0 or any(h[base + i] != h[len(h) - period + i] for i in range(period)):
                    break
                laps += 1
            if laps >= IDENTICAL_NOTICE_AT:
                return period, laps
        return None
