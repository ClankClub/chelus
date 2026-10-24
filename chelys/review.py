"""The background review: after enough tool-calling steps in the owner's conversation, a
second pass reads the conversation and improves the skill library; after enough owner
messages, it looks for facts worth keeping in memory. When both are due, one pass does both
with a combined prompt.

Rebuilt from Hermes Agent's background_review.py (Nous Research, MIT); notes in
docs/hermes/skills.md and docs/hermes/memory.md. As in Hermes:
- memory: it may only add; a patch or remove waits for the owner, pinned to the exact
  entry (memory.py); a correction about how a task is done goes to that task's skill;
- it runs after the reply is delivered, so it never delays the owner's answer;
- it replays the conversation with the same system prompt and tool list, so its first
  request reads the prefix cache warm;
- it may use only the tools for what it reviews (skill tools and read-only file tools,
  the memory tool); anything else is refused with a message naming what it may use;
- read-before-write is enforced (skill_tools): it must load a file in this review before
  changing it;
- at most 16 model steps; it never deletes.
Different on the phone:
- it runs in the caller's thread, right after the reply (the bridge handles the next
  message when it finishes) and holds the conversation's turn lease meanwhile;
- with skill_approval = "ask", its changes are staged as pending for the owner, except
  for skills the owner marked "always"; it skips its run while 10 changes already wait;
- it runs only on the owner's conversations, never a guest's.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import guardrails, memory, provider, skill_tools, tools
from .agent import Agent, _dedupe, _tool_row, conversation_prompt
from .store import SessionBusy

MAX_STEPS = 16
REVIEW_TOOLS = ("skills_list", "skill_view", "skill_manage", "read_file", "list_dir")

LESSONS = """\
What a skill IS: the instructions for doing a class of task the best way, to THIS owner's
liking: the procedure, the tools and commands that work, the order, how the owner wants the
result to look, and the pitfalls that cost time. A later conversation should be able to
follow it and get it right the first time. So:
- Procedure first: steps in the order they're done, with the concrete commands, tool calls
  and decision points. Pitfalls attach to the step they affect.
- A pitfall is a general rule plus one clause of WHY, in the imperative. Not a story of what
  happened this time.
- No dates, issue numbers or quoted chat as content: the rule must stand on its own.
- The same lesson twice is ONE rule. Check the skill for it before adding; sharpen the
  existing rule instead of appending a copy.
- Don't restate what's always in your instructions or the tool descriptions.
- Standing preferences that apply every time live in SKILL.md itself. references/ is for
  depth needed only sometimes, one file per topic, extended before a new one is added.
- When a skill is wrong, fix the sentence that misled; don't append 'UPDATE: actually...'."""

DO_NOT_CAPTURE = """\
Do NOT capture (these harden into rules that bite later, when things change):
- Failures that depend on the setup: a missing package or command, an app not installed,
  a credential not configured. The owner can fix those; they aren't lasting rules.
- Negative claims about tools ('run_shell can't do X', 'battery_status is broken'). They
  turn into refusals you cite against yourself long after the problem is fixed.
- Errors that went away within the conversation. If a retry worked, the lesson is the
  retry, not the error.
- One-off tasks. A single question isn't a class of work.
- Unresolved failures: if no working method was found, don't write the attempts up as a
  'reliable workflow'. Say 'Nothing to save.' instead, unless you are independently sure
  of a working alternative, and then capture only that.
If a tool failed because of setup, capture the FIX (the install command, the setting)
under a setup skill, never 'this tool doesn't work'."""

SKILL_BODY = f"""\
Be ACTIVE: most conversations produce at least one skill update,
even a small one. A pass that does nothing is a missed lesson, not a neutral outcome.

Aim for CLASS-LEVEL skills, each a SKILL.md of always-on rules plus a few topical
references/ files; not many narrow one-conversation skills.

{LESSONS}

Signals to act on (any one is enough):
- The owner corrected your style, tone, format or length ('too long', 'stop doing X', 'just
  answer'). That is a first-class skill signal: put the preference in the skill for that
  kind of task, so the next conversation starts knowing it.
- The owner corrected your workflow or the order of steps. Encode it as a step or pitfall.
- A non-trivial technique, fix or workaround emerged that would help next time.
- A skill you loaded turned out wrong, incomplete or outdated. Patch it now.

Preference order (pick the earliest that fits, but do pick one when a signal fired):
1. Update a skill loaded in this conversation with skill_view, if it covers the lesson.
2. Update an existing skill that covers this class of task (skills_list, then skill_view).
3. Add a support file under an existing skill (references/<topic>.md, templates/,
   scripts/) with skill_manage write_file, and add a one-line pointer to it in SKILL.md.
4. Only then create a new skill, named for the class of task. If the name only makes
   sense for today's task, it's wrong: go back to 1-3.

Read before write (ENFORCED, skill_manage refuses otherwise): before changing an existing
SKILL.md, call skill_view(name) in this review; before changing an existing support file,
call skill_view(name, file_path=...) for that file. Copies earlier in the conversation
don't count. Base the change on what skill_view just returned. If refused, view once and
retry once; don't loop.

Never delete a skill here. If two skills overlap, say so in your reply.

{DO_NOT_CAPTURE}

Your changes may be held for the owner to approve; that's expected, keep going.
'Nothing to save.' is a real answer but shouldn't be the default."""

PROMPT = f"""\
[Skill review] This is not a message from the owner. Review the conversation above and
update the skill library. {SKILL_BODY} If the conversation went smoothly with no
corrections and nothing new was learned, reply 'Nothing to save.' and stop. Otherwise, act,
then reply with one line per change."""


MEMORY_PROMPT = """\
Two memory stores; pick one per fact:
- user: who the owner is: name, situation, preferences, how they like to be talked to,
  things they said about themselves, corrections to how you talk with them.
- memory: this phone and your setup: paths, conventions, tool quirks that will still hold
  next week.
One fact goes to one store, once. Write facts, not orders ('Owner prefers metric units',
not 'Use metric'). Check the stores below first and don't save what's already there.
Skip anything only about this task, task progress, logs of finished work, things you can
look up again, one-off details, anything stale within a week, and guesses the owner didn't
state. Don't save that a tool or command doesn't work: setup problems get fixed, and the
note would keep you refusing long after. Here you may only add: a patch or remove you make waits for the owner's approval.
Current stores (live; may be newer than your instructions):
{stores}"""

MEMORY_ONLY = """\
[Memory review] This is not a message from the owner. Review the conversation above for
facts worth keeping in EVERY future conversation, and save them with the memory tool.

{memory}

If nothing stands out, reply 'Nothing to save.' and stop. Otherwise save, then reply
with one line per change."""

COMBINED = """\
[Review] This is not a message from the owner. Review the conversation above twice: once
to update the skill library, and once for facts worth keeping in memory.

Routing: a correction about how a kind of task should be done goes in that task's skill,
not memory. The user profile is only for preferences that cut across all tasks. Never put
one lesson in both.

SKILLS:
{skills}

MEMORY:
{memory}

If neither has anything, reply 'Nothing to save.' and stop. Otherwise act, then reply
with one line per change."""

MEMORY_TOOLS = ("memory",)


@dataclass
class Result:
    saved: list[str] = field(default_factory=list)       # skill writes applied directly ("saved: ...")
    remembered: list[str] = field(default_factory=list)  # memory writes applied directly
    staged: list[str] = field(default_factory=list)      # pending change ids (skills and memory)
    reply: str = ""
    skipped: str = ""                                     # why it didn't run, if it didn't
    kinds: tuple[str, ...] = ("skills",)

    def summary(self) -> str:
        label = {("skills",): "Skill review", ("memory",): "Memory review"}.get(self.kinds, "Review")
        if self.skipped:
            return f"{label} skipped: {self.skipped}"
        parts = []
        if self.remembered:
            parts.append("remembered " + "; ".join(self.remembered))
        if self.saved:
            parts.append("saved " + "; ".join(x.removeprefix("saved: ").split("\n")[0] for x in self.saved))
        if self.staged:
            parts.append(f"proposed {len(self.staged)} change(s) for your approval")
        return f"{label}: " + (", ".join(parts) if parts else "nothing to save") + "."


def _prompt(kinds: tuple[str, ...], s: Any) -> str:
    stores = memory.owner_view(s).rsplit("\n\nFiles:", 1)[0] if "memory" in kinds else ""
    mem = MEMORY_PROMPT.format(stores=stores)
    if kinds == ("skills",):
        return PROMPT
    if kinds == ("memory",):
        return MEMORY_ONLY.format(memory=mem)
    return COMBINED.format(skills=SKILL_BODY, memory=mem)


def run(parent: Agent, kinds: tuple[str, ...] | None = None) -> Result:
    """Run one review of parent's conversation: skills, memory or both, as parent's turn
    made due (skills alone when called with neither due). Never raises for model or busy
    errors; the reply has already gone out, and a failed review just means no changes."""
    store, sid, s = parent.store, parent.session_id, parent.s
    if kinds is None:
        kinds = tuple(k for k, due in (("skills", parent.review_due), ("memory", parent.memory_review_due)) if due)
        kinds = kinds or ("skills",)
    if parent.guest:
        return Result(skipped="guest conversation", kinds=kinds)
    waiting = len(store.pending())
    if waiting >= skill_tools.PENDING_CAP and "skills" in kinds:
        if kinds == ("skills",):
            return Result(skipped=f"{waiting} earlier changes still wait for your approval (/pending)", kinds=kinds)
        kinds = ("memory",)  # memory adds need no approval, so that part can still run
    allowed = (REVIEW_TOOLS if "skills" in kinds else ()) + (MEMORY_TOOLS if "memory" in kinds else ())
    rev = Agent(s, parent.api_key, store, sid, approve=lambda req: False, on_event=parent.on_event,
                chat_fn=parent.chat_fn, hard_stop=True, actor="review")
    # Same prompt and tools as the conversation, for a warm prefix cache (Hermes).
    rev.system = conversation_prompt(s, store, sid)
    schemas = tools.schemas(rev.tool_names)
    holder = f"pid={os.getpid()}:{secrets.token_hex(3)}"
    try:
        store.acquire_lease(sid, holder)
    except SessionBusy as e:
        return Result(skipped=str(e), kinds=kinds)
    rev._lease = holder
    result = Result(kinds=kinds)
    try:
        history = store.messages(sid)
        turn: list[dict[str, Any]] = [{"role": "user", "content": _prompt(kinds, s)}]
        rev._history, rev._turn = history, turn
        guard = guardrails.Guard(True)
        parent.on_event("notice", f"running the background review ({' and '.join(kinds)})")
        for _ in range(MAX_STEPS):
            msg = rev._call(history + turn, schemas, None)
            finish = msg.pop("_finish_reason", None)
            calls = msg.get("tool_calls") or []
            if calls and finish == "length":
                result.reply = "(review stopped: a tool call was cut off by max_tokens)"
                break
            if not calls:
                result.reply = (msg.get("content") or "").strip()
                break
            msg["tool_calls"] = _dedupe(calls)
            turn.append(msg)
            for call in msg["tool_calls"]:
                name = call["function"]["name"]
                if guard.halt is not None:
                    out = "not run: the review was stopped by the tool-loop guard"
                elif name not in allowed:
                    out = (f"denied: this review may only use {', '.join(allowed)}. Don't retry this tool.")
                else:
                    out = rev._run_tool(call, guard)
                if name == "skill_manage" and out.startswith("saved:"):
                    result.saved.append(out)
                turn.append(_tool_row(call, out))
            if guard.halt is not None:
                result.reply = f"(review stopped by the loop guard: {guard.halt.message})"
                break
        else:
            result.reply = f"(review hit its {MAX_STEPS}-step limit)"
    except (provider.ProviderError, SessionBusy) as e:
        result.reply = f"(review failed: {e})"
    finally:
        rev._lease = None
        try:
            store.release_lease(sid, holder)
        except sqlite3.Error:
            pass
    result.staged = list(rev.staged)
    result.remembered = list(rev.memory_notes)
    parent.on_event("notice", result.summary())
    return result
