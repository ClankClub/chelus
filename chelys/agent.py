"""The agent loop: ask the model, run the tools it asks for, send results back, repeat
until it answers in plain text or a limit is hit.

The loop's rules follow Hermes Agent (Nous Research, MIT); docs/hermes/brain_summary.md maps
each piece to its Hermes source. In short:
- the system prompt is built once and never changes; the time rides in the user message,
  stored with the exact bytes sent so later turns replay them (prefix-cache hits)
- rows are saved as they happen; a tool-call row is saved before its tools run, and a turn
  that dies midway is closed with honest results and a note (persist as you go)
- older history that would no longer fit is summarized, not dropped (compaction)
- a tool call cut off by max_tokens is never run: retry with a bigger max_tokens, then stop
- a text reply cut off by max_tokens is continued (up to 3 times) and saved as one reply
- duplicate calls in one reply are dropped; an empty reply after tool results gets one nudge
- a per-turn guard warns about, and on Telegram stops, tool calls repeating without progress
- at the step limit the model gets one last call to summarize instead of a dead stop
- on Telegram, an outage that outlasts the retries is waited out with a countdown
- one turn at a time per conversation, across processes (a lease in the database)
Every recovery is bounded, and scaffolding messages (nudges, fragments) are not saved.

Skills (docs/hermes/skills.md): the owner's prompt carries the skill index, frozen per
conversation so the prompt stays byte-stable; when the library changes mid-conversation,
the next user message carries a one-time note instead. Guests get no skill tools and no
index. After enough tool-calling steps, the caller runs the background review (review.py).

Memory (docs/hermes/memory.md): the owner's prompt also carries both memory stores, frozen
per conversation the same way and refreshed at compaction, plus the guidance for the memory
and session_search tools. After 10 owner messages the review also looks for facts to keep.
Guests get neither.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import sqlite3
import time
from importlib import resources
from typing import Any, Callable

# skill_tools, memory and session_search register their tools on import
from . import approval, compaction, guardrails, memory, provider, session_search, skill_tools, skills, tools  # noqa: F401
from .config import Settings
from .repetition import is_repetition_dominated
from .store import Store, history_chars, recent_window

Approve = Callable[[str], bool]
Event = Callable[[str, str], None]
# Waits out one auto-recovery cycle: (wait_s, cycle, total, error) -> False if the owner stopped it.
Recover = Callable[[float, int, int, str], bool]

TRUNCATED_TOOL_RETRIES = 4  # boosted-max_tokens retries for a cut-off tool call (Hermes: 4)
MAX_CONTINUATIONS = 4       # a cut-off text reply is continued until the 4th cut (Hermes: 4)
STREAK_HALTS = ("identical_call_streak_halt", "identical_cycle_halt")

# Auto-recovery ladder (Hermes turn_recovery_autorecover): 15/30/60/60/60 s plus 0-20% jitter;
# a server's Retry-After wins, up to 120 s.
LADDER_BASE_S, LADDER_CAP_S, LADDER_JITTER, LADDER_RETRY_AFTER_CAP_S = 15.0, 60.0, 0.2, 120.0

CONTINUE_NUDGE = (
    "[System: Your previous response was truncated by the output length limit. Continue exactly "
    "where you left off. Do not restart or repeat prior text. Finish the answer directly.]"
)
EMPTY_AFTER_TOOLS_NUDGE = (
    "You just executed tool calls but returned an empty response. Please process the tool "
    "results above and continue with the task."
)
SUMMARY_REQUEST = (
    "You've reached the maximum number of tool-calling iterations allowed. Please provide a final "
    "response summarizing what you've found and accomplished so far, without calling any more tools."
)
TRUNCATED_FINAL = (
    "The model's reply was cut off before it finished (it hit its output length limit), so I "
    "didn't run the incomplete action. Send `continue`, ask for the work in smaller steps, or "
    "raise max_tokens in config.toml."
)
REPETITION_FINAL = (
    "The model's reply got stuck repeating itself, so I dropped it. "
    "Send `continue` to try again, or rephrase the request."
)
CEILING_NO_TEXT = (
    "The model hit its output length limit on every continuation attempt without producing "
    "any text. Raise max_tokens in config.toml, or ask for less at once."
)
EMPTY_FINAL = "{model} returned an empty reply. Send `continue` to try again."
NO_SUMMARY = (
    "I ran out of steps for this turn ({limit} model steps) before finishing, and couldn't "
    "produce a summary. Send `continue` to keep going, or raise max_iterations in config.toml."
)
EMPTY_SUMMARY = "I reached the step limit and couldn't generate a summary."
HALT_FINAL = (
    "I stopped because I kept running {tool} {count} times without making progress. "
    "Tell me how you'd like to proceed, or send `continue` and I'll try a different approach."
)
# A crash leaves a turn open; the next turn closes it with these. "May or may not": the
# process can die after a tool ran but before its result was saved.
CRASH_RESULT = ("no result recorded: Chelys stopped in the middle of this turn, so this call may or "
                "may not have run. Check before relying on it.")
CRASH_NOTE = "[this turn was interrupted: Chelys stopped before it finished{tools}]"
MAY_HAVE_RUN = ". Some actions may already have run; check their effects before resending"


def approval_ok(answer: object) -> bool:
    return approval.decision(answer) != approval.DENY


def load_persona(s: Settings) -> str:
    if s.persona_path.exists():
        return s.persona_path.read_text(encoding="utf-8").strip()
    return resources.files("chelys").joinpath("persona_default.md").read_text(encoding="utf-8").strip()


def disabled_tools(s: Settings, store: Store | None = None) -> tuple[str, ...]:
    """Tools switched off by config, or unavailable here (session search needs FTS5)."""
    off = []
    if not s.memory:
        off.append("memory")
    if not s.session_search or (store is not None and not store.fts):
        off.append("session_search")
    return tuple(off)


def system_prompt(s: Settings, guest: bool = False, skills_index: str | None = None,
                  memory_snapshot: str | None = None, search: bool = False) -> str:
    """Byte-stable for a given config, frozen skill index and frozen memory snapshot:
    nothing here may change between turns (no clock), or DeepSeek's prefix cache misses on
    the whole history. Order follows Hermes: identity and rules, then skills, then memory."""
    names = tools.available(guest)
    risky = ", ".join(n for n in names if tools.TOOLS[n].risky)
    prompt = (
        f"{load_persona(s)}\n\n"
        "## Where you are\n"
        f"You run inside Termux on a small Android phone ({platform.machine()}, Python {platform.python_version()}). "
        f"Your tools act on this phone. Working directory: {s.workdir}. "
        "Each user message ends with the local time it was sent.\n"
        f"The owner must approve {risky} before they run; a denial is final for that call, so don't retry it. "
        "Fill in `purpose` plainly: the owner decides from it on a small screen. "
        f"You get at most {s.max_iterations} model steps per message, and tool output is cut off after "
        f"{s.tool_output_chars} characters. Keep replies short: the owner may be reading on a phone."
    )
    if guest:
        prompt += ("\n\nYou are talking with a guest the owner invited, not the owner. Risky tools are off for "
                   "guests, and you have no skills or memory in this conversation.")
        return prompt
    if skills_index is not None:
        prompt += "\n\n" + skills.prompt_section(skills_index)
    recall = memory.prompt_section(memory_snapshot if s.memory else None, search)
    if recall:
        prompt += "\n\n" + recall
    return prompt


def conversation_prompt(s: Settings, store: Store, session_id: int) -> str:
    """The owner's prompt for a conversation, from its frozen skill index and memory
    snapshot: what the background review must replay to read the prefix cache warm."""
    return system_prompt(s, False, store.session_field(session_id, "skills_index") or "",
                         store.session_field(session_id, "memory_snapshot") or "",
                         "session_search" not in disabled_tools(s, store))


LEARN_STEPS = 16  # a /learn turn reads sources and writes several files; Hermes gives it a normal turn's budget


def stamp_user_message(text: str, when: float | None = None, api_text: str | None = None) -> dict[str, Any]:
    """The user row: clean text for display, plus the exact bytes sent to the model
    (api_text when the model should see more than what was typed, as with /learn)."""
    sent = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(when))
    return {"role": "user", "content": text, "api_content": f"{api_text or text}\n\n[sent {sent}]"}


def ladder_wait(cycle: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after, LADDER_RETRY_AFTER_CAP_S)
    return provider.jittered_backoff(cycle, LADDER_BASE_S, LADDER_CAP_S, LADDER_JITTER)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [cut off: {len(text) - limit} more characters]"


def _join_parts(parts: list[str]) -> str:
    """Join continuation fragments; add a newline only where neither side has whitespace."""
    joined = ""
    for part in parts:
        if joined and part and not joined[-1].isspace() and not part[0].isspace():
            joined += "\n"
        joined += part
    return joined


def _tool_row(call: dict[str, Any], content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call["id"], "content": content}


def _unanswered_calls(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool calls in the last tool-calling assistant row that have no result row after it."""
    idx = next((i for i in range(len(rows) - 1, -1, -1)
                if rows[i].get("role") == "assistant" and rows[i].get("tool_calls")), None)
    if idx is None:
        return []
    answered = {m.get("tool_call_id") for m in rows[idx + 1:] if m.get("role") == "tool"}
    return [c for c in rows[idx]["tool_calls"] if c["id"] not in answered]


def _parse_args(name: str, raw: str) -> tuple[dict[str, Any] | None, str]:
    """(args, "") or (None, error text for the model)."""
    if name not in tools.TOOLS:
        return None, f"error: no tool named {name!r}. Available tools: {', '.join(tools.TOOLS)}"
    raw = (raw or "").strip() or "{}"
    try:
        args = json.loads(raw)
    except ValueError as e:
        if not raw.endswith("}"):
            return None, (f"error: the arguments for {name} were cut off before the end (incomplete JSON). "
                          "Write less in one call; for example, split a large file into several writes.")
        return None, f"error: bad arguments for {name}: {e}"
    if not isinstance(args, dict):
        return None, f"error: bad arguments for {name}: arguments must be a JSON object"
    return args, ""


def _dedupe(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeats of the same tool with the same arguments in one reply; first wins.
    Arguments are compared in canonical form, so key order can't hide a duplicate."""
    seen: set[tuple[str, str]] = set()
    out = []
    for call in calls:
        fn = call["function"]
        raw = fn.get("arguments") or ""
        try:
            key = guardrails.canonical_args(json.loads(raw or "{}"))
        except ValueError:
            key = raw
        sig = (fn["name"], key)
        if sig not in seen:
            seen.add(sig)
            out.append(call)
    return out


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
        hard_stop: bool = False,
        autorecover: Recover | None = None,
        guest: bool = False,
        actor: str = "foreground",
        reusable_approvals: bool = True,
    ):
        self.s = s
        self.api_key = api_key
        self.store = store
        self.session_id = session_id
        self.approve = approve
        self.on_event = on_event or (lambda kind, text: None)
        self.chat_fn = chat_fn
        self.hard_stop = hard_stop  # let the loop guard stop a turn (unattended surfaces)
        self.autorecover = autorecover  # wait out outages (unattended surfaces)
        self.guest = guest          # a Telegram guest: no risky tools, no skills, confined file tools
        self.actor = actor          # "foreground", or "review" for the background skill review
        self.reusable_approvals = reusable_approvals
        self.tool_names = tools.available(guest, disabled_tools(s, store))
        # The prompt is rebuilt from the conversation's frozen skill index and memory snapshot
        # at each turn start: the same bytes every turn until compaction refreshes both.
        self.system = system_prompt(s, guest, None if guest else "")
        self.tools_ok: list[str] = []       # tools that ran and returned normally, latest turn
        self.tools_started: list[str] = []  # tools whose handler was invoked, latest turn
        self.viewed: set[tuple[str, str]] = set()  # (skill, file) loaded with skill_view; the review's read-before-write
        self.staged: list[str] = []         # pending change ids this agent proposed (background review)
        self.review_due = False             # the caller should run the background skill review after replying
        self.memory_review_due = False      # ... and/or the memory review
        self.memory_failures = 0            # failed memory writes this turn (the tool stops at 3)
        self.memory_notes: list[str] = []   # memory writes this turn, for the reply's notice
        self.step_limit = s.max_iterations
        self._history: list[dict[str, Any]] = []
        self._turn: list[dict[str, Any]] = []
        self._turn_no: int | None = None
        self._lease: str | None = None
        self._recoveries = 0
        self._compacted = False

    @property
    def reviews(self) -> bool:
        """Whether this conversation feeds the background skill review: owner only."""
        return not self.guest and self.actor == "foreground" and self.s.review_interval > 0

    def run_turn(self, user_text: str, api_text: str | None = None, step_limit: int | None = None) -> str:
        """Run one user message to a final reply, saving rows as they happen.

        api_text, if given, is what the model sees instead of user_text (/learn sends its
        whole prompt while the history shows what was typed). step_limit overrides
        max_iterations for this turn.

        Ctrl-C or a model/API failure closes the turn: calls without results get one saying
        they didn't run, and a note says what happened, so the next turn knows.
        Raises store.SessionBusy if another process is mid-turn in this conversation.
        """
        user_msg = stamp_user_message(user_text, api_text=api_text)  # the turn's clock is frozen here
        self.tools_ok, self.tools_started = [], []
        self._turn_no, self._recoveries = None, 0
        self.review_due = self.memory_review_due = False
        self.memory_failures, self.memory_notes = 0, []
        self.step_limit = max(step_limit or 0, self.s.max_iterations)
        holder = f"pid={os.getpid()}:{secrets.token_hex(3)}"
        self.store.acquire_lease(self.session_id, holder)
        self._lease = holder
        turn: list[dict[str, Any]] = []
        try:
            history = self._close_open_turn(self.store.messages(self.session_id))
            self._compacted = False
            history = self._maybe_compact(history, user_msg)
            self._prepare_prompt(user_msg, refresh=self._compacted)
            self._turn_no = self.store.last_turn(self.session_id) + 1
            self._add(turn, user_msg)
            reply = self._loop(history, turn)
            self._check_review_due()
            self._check_memory_review_due()
            return reply
        except KeyboardInterrupt:
            self._close_turn(turn, user_msg, "cancelled by the owner (Ctrl-C)")
            raise
        except provider.ProviderError as e:
            self._close_turn(turn, user_msg, f"failed: {e}")
            raise
        finally:
            self._lease = None
            try:
                self.store.release_lease(self.session_id, holder)
            except sqlite3.Error:
                pass

    # ---- saving ----

    def _add(self, turn: list[dict[str, Any]], row: dict[str, Any]) -> None:
        """Add a row to the turn and, unless it is scaffolding, save it now."""
        turn.append(row)
        if not row.get("_scaffold"):
            self.store.append(self.session_id, self._turn_no, [row])

    def _close_turn(self, turn: list[dict[str, Any]], user_msg: dict[str, Any], why: str) -> None:
        """End a turn that failed or was cancelled. Never claims an action didn't happen when
        a tool may have run (Hermes's failed_turn_notice)."""
        try:
            if self._turn_no is None:
                self._turn_no = self.store.last_turn(self.session_id) + 1
            saved = [m for m in turn if not m.get("_scaffold")]
            if not saved:
                self._add(turn, user_msg)
            for call in _unanswered_calls(saved):
                self._add(turn, _tool_row(call, f"not run: the turn ended first ({why})"))
            note = f"[this turn {why}"
            if self.tools_started:
                note += f"; tools that already ran: {', '.join(self.tools_started)}{MAY_HAVE_RUN}"
            self._add(turn, {"role": "assistant", "content": note + "]"})
        except sqlite3.Error:
            pass  # never let a failed save hide the error that ended the turn

    def _close_open_turn(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Close a turn that an earlier process left open (it died mid-turn). Every tool call
        gets a result and the turn gets a closing note, so the history stays well-formed."""
        if not history:
            return history
        last = history[-1]
        if last.get("role") == "assistant" and not last.get("tool_calls"):
            return history
        start = max((i for i, m in enumerate(history) if m.get("role") == "user"), default=0)
        open_rows = history[start:]
        fix = [_tool_row(c, CRASH_RESULT) for c in _unanswered_calls(open_rows)]
        called = any(m.get("tool_calls") for m in open_rows)
        fix.append({"role": "assistant", "content": CRASH_NOTE.format(tools=MAY_HAVE_RUN if called else "")})
        self.store.append(self.session_id, self.store.last_turn(self.session_id), fix)
        self.on_event("notice", "closed a turn an earlier run left unfinished")
        return history + fix

    # ---- skills and memory in the prompt ----

    def _prepare_prompt(self, user_msg: dict[str, Any], refresh: bool) -> None:
        """Freeze this conversation's skill index and memory snapshot into the prompt, and
        note skill changes since.

        Both are stored on the session row: the Telegram bridge builds a new Agent for every
        message, so an in-memory snapshot would change the prompt (and lose the prefix
        cache) whenever the library or memory did. They are refreshed only when compaction
        has already rewritten the history, as Hermes does. Memory written meanwhile is on
        disk at once and in the conversation as the tool call that wrote it."""
        if self.guest:
            self.system = system_prompt(self.s, True)
            return
        try:
            current = skills.discover(skills.root(self.s))
        except OSError:
            current = []
        frozen = self.store.session_field(self.session_id, "skills_index")
        if frozen is None or refresh:
            frozen = skills.index_text(current)
            self.store.set_session_field(self.session_id, "skills_index", frozen)
            self.store.set_session_field(self.session_id, "skills_noted", "")
        snap = self.store.session_field(self.session_id, "memory_snapshot")
        if self.s.memory and (snap is None or refresh):
            snap = memory.snapshot(self.s)
            self.store.set_session_field(self.session_id, "memory_snapshot", snap)
        self.system = system_prompt(self.s, False, frozen, snap or "", "session_search" in self.tool_names)
        note = skills.change_note(frozen, skills.index_map(current))
        noted = self.store.session_field(self.session_id, "skills_noted") or ""
        if note == noted:
            return
        self.store.set_session_field(self.session_id, "skills_noted", note)
        if not note:
            note = "[System: the skill list in your instructions is accurate again; ignore earlier skill-change notes.]"
        user_msg["api_content"] += "\n\n" + note

    def _check_review_due(self) -> None:
        if not self.reviews:
            return
        steps = int(self.store.session_field(self.session_id, "review_steps") or 0)
        if steps >= self.s.review_interval:
            self.store.set_session_field(self.session_id, "review_steps", 0)
            self.review_due = True

    def _check_memory_review_due(self) -> None:
        """Count owner messages toward the memory review (Hermes: every 10 user turns). A
        turn in which the model saved to memory itself resets the count."""
        if self.guest or self.actor != "foreground" or not self.s.memory or self.s.memory_review_interval <= 0:
            return
        try:
            turns = 0 if "memory" in self.tools_ok else int(self.store.session_field(self.session_id, "memory_turns") or 0) + 1
            if turns >= self.s.memory_review_interval:
                turns, self.memory_review_due = 0, True
            self.store.set_session_field(self.session_id, "memory_turns", turns)
        except sqlite3.Error:
            pass

    def _count_review_step(self, calls: list[dict[str, Any]]) -> None:
        """One tool-calling model step toward the next review; the model saving a skill
        itself resets the count (Hermes's creation nudge counter)."""
        if not self.reviews:
            return
        try:
            if any(c["function"]["name"] == "skill_manage" for c in calls):
                self.store.set_session_field(self.session_id, "review_steps", 0)
            else:
                steps = int(self.store.session_field(self.session_id, "review_steps") or 0)
                self.store.set_session_field(self.session_id, "review_steps", steps + 1)
        except sqlite3.Error:
            pass

    # ---- compaction ----

    def _maybe_compact(self, history: list[dict[str, Any]], user_msg: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.s.compaction:
            return history
        span = compaction.plan(history, self.s.history_chars, history_chars([user_msg]))
        if span is None or time.time() < self.store.compact_retry_at(self.session_id):
            return history
        head_end, tail_start = span
        middle = history[head_end:tail_start]
        budget = compaction.summary_budget_tokens(middle, self.s.history_chars)
        prompt = compaction.build_prompt(compaction.serialize(middle, history), budget,
                                         compaction.previous_summary(middle), compaction.today())
        self.on_event("notice", f"summarizing {len(middle)} older messages to keep the history in budget")
        self._renew()
        try:
            text = self._summarize(prompt)
            if len(text) > compaction.summary_cap_chars(budget):
                self.on_event("notice", f"summary is {len(text)} chars, over its budget; condensing it")
                self._renew()
                text = self._summarize(compaction.condense_prompt(text, budget))
        except provider.ProviderError as e:
            return self._compaction_failed(history, str(e))
        if not text:
            return self._compaction_failed(history, "the summary came back empty or cut off")
        if len(text) > compaction.summary_cap_chars(budget):
            return self._compaction_failed(history, f"the summary is still {len(text)} chars after condensing")
        text = compaction.add_pruned_skills(compaction.redact(text), middle, history[tail_start:])
        new = compaction.assemble(history, head_end, tail_start, text)
        if history_chars(new) >= history_chars(history):
            # Hermes rejects a compaction that would grow the transcript before committing it.
            return self._compaction_failed(history, "the summary would not have made the history smaller")
        self.store.replace_active(self.session_id, new)
        self._compacted = True
        self.on_event("notice", f"history summarized: {history_chars(history)} -> {history_chars(new)} chars")
        return new

    def _summarize(self, prompt: str) -> str:
        """One summary call; "" when it comes back empty or cut off. No tools and no real
        output cap: Hermes leaves max_tokens off summary calls, because a hard cap truncates
        summaries."""
        msg = self.chat_fn(self.s, self.api_key, [{"role": "user", "content": prompt}], [],
                           max_tokens=self.s.max_tokens_limit, on_retry=self._on_retry)
        usage = msg.pop("_usage", None)
        if isinstance(usage, dict):
            self._log_usage(usage)
        if msg.get("_finish_reason") == "length":
            return ""
        return (msg.get("content") or "").strip()

    def _compaction_failed(self, history: list[dict[str, Any]], why: str) -> list[dict[str, Any]]:
        """History stays exactly as it was; compaction pauses (Hermes's failure cooldown)."""
        self.store.set_compact_retry_at(self.session_id, time.time() + compaction.FAILURE_COOLDOWN_S)
        self.on_event("notice", f"couldn't summarize older history ({why}); trying again in "
                                f"{compaction.FAILURE_COOLDOWN_S:.0f}s")
        return history

    # ---- the loop ----

    def _loop(self, history: list[dict[str, Any]], turn: list[dict[str, Any]]) -> str:
        schemas = tools.schemas(self.tool_names)
        guard = guardrails.Guard(self.hard_stop)
        self._history, self._turn = history, turn
        parts: list[str] = []  # text fragments of a reply cut off by max_tokens
        cap: int | None = None  # boosted max_tokens while retrying a cut-off tool call
        trunc_retries = continuations = 0
        empty_nudged = False
        for _ in range(self.step_limit):
            msg = self._call(history + turn, schemas, cap)
            finish = msg.pop("_finish_reason", None)
            calls = msg.get("tool_calls") or []

            if calls and finish == "length":
                # Never run a cut-off call. Retry the same request with more room, but only
                # while the room can still grow: an identical retry would fail identically.
                bigger = self._boosted_cap(trunc_retries + 1)
                if trunc_retries < TRUNCATED_TOOL_RETRIES and bigger > (cap or self.s.max_tokens):
                    trunc_retries += 1
                    cap = bigger
                    self.on_event("notice", f"tool call cut off by max_tokens; retrying with max_tokens={cap}")
                    continue
                return self._end(turn, TRUNCATED_FINAL)
            if calls:
                cap, trunc_retries = None, 0
                self._count_review_step(calls)
                halt = self._tool_round(msg, turn, guard)
                if halt is not None:
                    return self._end(turn, HALT_FINAL.format(tool=halt.tool, count=halt.count))
                continue

            content = msg.get("content") or ""
            if finish == "length":
                if is_repetition_dominated(content):
                    return self._end(turn, REPETITION_FINAL)
                if content.strip():
                    msg["_scaffold"] = True
                    turn.append(msg)
                    parts.append(content)
                continuations += 1
                if continuations < MAX_CONTINUATIONS:
                    self.on_event("notice", f"reply cut off by max_tokens; asking it to continue ({continuations}/3)")
                    turn.append({"role": "user", "content": CONTINUE_NUDGE, "_scaffold": True})
                    continue
                if parts:
                    return self._end(turn, _join_parts(parts) + "\n[reply cut off: still hitting max_tokens]")
                return self._end(turn, CEILING_NO_TEXT)

            if not content.strip():
                if not empty_nudged and any(m.get("role") == "tool" for m in turn[-5:]):
                    empty_nudged = True
                    self.on_event("notice", "empty reply after tool results; nudging once")
                    turn.append({"role": "assistant", "content": "(empty)", "_scaffold": True})
                    turn.append({"role": "user", "content": EMPTY_AFTER_TOOLS_NUDGE, "_scaffold": True})
                    continue
                if not parts:
                    return self._end(turn, EMPTY_FINAL.format(model=self.s.model))
            msg["content"] = _join_parts(parts + [content])
            self._add(turn, msg)
            return msg["content"]
        return self._grace_call(turn, history, schemas)

    def _end(self, turn: list[dict[str, Any]], text: str) -> str:
        self._add(turn, {"role": "assistant", "content": text})
        return text

    def _grace_call(self, turn: list[dict[str, Any]], history: list[dict[str, Any]], schemas: list) -> str:
        """Out of steps: one more call asking for a summary instead of a dead stop.
        Tools stay in the request so the cached prefix still matches; any tool calls the
        model makes anyway are ignored. One retry if the summary comes back empty."""
        self.on_event("notice", f"hit the limit of {self.step_limit} model steps; asking for a summary")
        self._add(turn, {"role": "user", "content": SUMMARY_REQUEST})
        text = ""
        try:
            for _ in range(2):
                msg = self._call(history + turn, schemas, None)
                text = (msg.get("content") or "").strip()
                if text:
                    break
        except provider.ProviderError:
            return self._end(turn, NO_SUMMARY.format(limit=self.step_limit))
        return self._end(turn, text or EMPTY_SUMMARY)

    def _call(self, msgs: list[dict[str, Any]], schemas: list, max_tokens: int | None) -> dict[str, Any]:
        messages = [{"role": "system", "content": self.system}] + recent_window(msgs, self.s.history_chars)
        while True:
            self._renew()
            try:
                msg = self.chat_fn(self.s, self.api_key, messages, schemas, max_tokens=max_tokens,
                                   on_retry=self._on_retry)
                break
            except provider.ProviderError as e:
                self._recover_or_raise(e)
        usage = msg.pop("_usage", None)
        if isinstance(usage, dict):
            self._log_usage(usage)
        return msg

    def _recover_or_raise(self, e: provider.ProviderError) -> None:
        """After retries run out on an outage, wait and try the same call again, a bounded
        number of times (Hermes's auto-recovery ladder). The call is retried, not the turn,
        so tools that already ran never run twice. Raises when recovery doesn't apply."""
        total = self.s.autorecover_cycles
        if self.autorecover is None or not e.transient or total <= 0:
            raise e
        if self._recoveries >= total:
            raise provider.ProviderError(f"{e}; automatic recovery gave up after {total} cycles") from None
        self._recoveries += 1
        wait = ladder_wait(self._recoveries, e.retry_after)
        self.on_event("notice", f"provider unavailable; recovery cycle {self._recoveries}/{total} in {wait:.0f}s")
        if not self.autorecover(wait, self._recoveries, total, str(e)):
            raise provider.ProviderError(f"{e}; automatic retry cancelled with /stop") from None

    def _renew(self) -> None:
        if self._lease is not None:
            try:
                self.store.renew_lease(self.session_id, self._lease)
            except sqlite3.Error:
                pass

    def _boosted_cap(self, n: int) -> int:
        """max_tokens for cut-off retry n: base * 2^n, at most max(32768, 2 * base),
        never above max_tokens_limit (Hermes's boosted_output_cap with a known model limit)."""
        base = self.s.max_tokens
        return min(base * 2 ** n, max(32768, base * 2), max(self.s.max_tokens_limit, base))

    def _on_retry(self, attempt: int, max_retries: int, wait: float, error: str) -> None:
        text = f"attempt {attempt}/{max_retries} failed: {error}; retrying in {wait:.0f}s"
        self.on_event("wait" if wait >= provider.LONG_WAIT_S else "retry", text)

    def _log_usage(self, usage: dict[str, Any]) -> None:
        try:
            self.store.log_usage(self.session_id, usage)
        except sqlite3.Error:
            pass  # a failed usage write never costs the user their turn
        hit = usage.get("prompt_cache_hit_tokens")
        line = f"{usage.get('prompt_tokens', '?')} in"
        if isinstance(hit, int):
            line += f" ({hit} cached)"
        self.on_event("usage", f"{line}, {usage.get('completion_tokens', '?')} out")

    # ---- tools ----

    def _tool_round(self, msg: dict[str, Any], turn: list[dict[str, Any]], guard: guardrails.Guard):
        """Run one reply's tool calls. The call row is saved before anything runs, so a
        crash mid-tool still leaves a record of what was attempted. Every call gets a
        result row, even ones not run. Returns the guard's halt decision, if it stopped
        the turn."""
        msg["tool_calls"] = _dedupe(msg["tool_calls"])
        self._add(turn, msg)
        for call in msg["tool_calls"]:
            if guard.halt is not None:
                self._add(turn, _tool_row(call, "not run: the turn was stopped by the tool-loop guard"))
                continue
            self._add(turn, _tool_row(call, self._run_tool(call, guard)))
            self._renew()
        return guard.halt

    def _run_tool(self, call: dict[str, Any], guard: guardrails.Guard) -> str:
        name = call["function"]["name"]
        if name in tools.TOOLS and name not in self.tool_names:
            return f"error: no tool named {name!r}. Available tools: {', '.join(self.tool_names)}"
        args, err = _parse_args(name, call["function"].get("arguments") or "")
        if args is None:
            return err
        pre = guard.before_call(name, args)
        if not pre.allows_execution:
            self.on_event("guard", pre.message)
            return f"blocked: {pre.message}"
        out, failed = self._execute(name, args)
        halt_before = guard.halt
        decision = guard.after_call(name, args, out, failed)
        notice, stub = guard.observe_call(name, args, out, call["id"], failed)
        if name == "skill_view" and not failed and self.actor != "review" and self._already_shown(out):
            # Hermes's repeat-view dedup. Never in the review, which must load fresh copies.
            stub = (f"[skill_view: unchanged since it was loaded earlier in this conversation; use that "
                    f"copy, it is still in your context]")
        content = stub or _clip(out, tools.TOOLS[name].max_output or self.s.tool_output_chars)
        if decision.action in ("warn", "halt"):
            content += decision.guidance()
            self.on_event("guard", decision.message)
        if (decision.action != "halt" and guard.halt is not halt_before
                and guard.halt is not None and guard.halt.code in STREAK_HALTS):
            content += guard.halt.guidance()
            self.on_event("guard", guard.halt.message)
        if notice:
            content += "\n\n" + notice
        return content

    def _already_shown(self, out: str) -> bool:
        """True if this exact tool result is already in what the model sees."""
        window = recent_window(self._history + self._turn, self.s.history_chars)
        return any(m.get("role") == "tool" and m.get("content") == out for m in window)

    def _execute(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run one tool. Returns (output, failed)."""
        tool = tools.TOOLS[name]
        if self.guest and tool.risky:
            return "denied: risky tools are for the owner only", True
        args = dict(args)
        reusable = self.reusable_approvals and self.actor == "foreground" and name in ("run_shell", "write_file")
        key = ""
        if reusable:
            try:
                key = tools.action_key(self.s, name, args, self)
            except (tools.ToolError, OSError) as e:
                return f"error: {e}", True
        summary = tools.describe_call(name, args)
        choice = approval.ONCE
        if tool.risky:
            if reusable and self.store.tool_granted(key, self.session_id):
                choice = approval.ONCE
            else:
                choice = approval.decision(self.approve(summary))
            if choice == approval.SESSION and reusable:
                self.store.grant_tool("*", self.session_id, "ordinary tools")
            elif choice == approval.ALWAYS and reusable:
                self.store.grant_tool(key, 0, name)
        if tool.risky and choice == approval.DENY:
            self.on_event("denied", summary)
            return "denied: the owner did not approve this action", True
        self.on_event("tool", summary)
        self.tools_started.append(name)
        try:
            out = tool.handler(self.s, args, self)
            failed = (name == "run_shell" and not out.startswith("exit code 0")) or out.startswith("denied:")
            if not out.startswith("denied:"):
                self.tools_ok.append(name)
        except (tools.ToolError, OSError) as e:
            out, failed = f"error: {e}", True
        self.on_event("result", out.splitlines()[0][:120] if out else "(empty)")
        return out, failed
