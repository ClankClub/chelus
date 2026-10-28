# Hermes memory subsystem: reading notes

Source: NousResearch/hermes-agent @ 99fcd69, MIT. Notes on behavior and numbers; no code copied.

## What memory is, in Hermes's own framing

Hermes splits what it learns three ways, and the split is the design:

- **Memory** (`MEMORY.md`, `USER.md`): a few small facts that apply to *every* session, injected into the system prompt every turn. Hard character budget.
- **Skills**: how to do a class of task, loaded only when relevant (see [skills.md](skills.md)). Procedures, pitfalls, and the user's preferences *for that kind of work* go here, not in memory.
- **Session search**: everything else. The full history in SQLite with FTS5, searched on demand at no prompt cost.

Hermes's own docs say the whole thing pays off at **session boundaries**: memory carries the essentials into the next session, and session search fills the gaps once old context is gone. A Telegram chat that never ends a session barely uses either. Hermes's answer is to tell users to `/new` at natural breaks, and to reload memory when compaction rebuilds the prompt.

Everything beyond the two files and session search is a plugin layer for external memory backends (Honcho, mem0, RetainDB, ByteRover and others), which a phone doesn't need.

## Files read

| File | Lines | What it holds |
|---|---|---|
| `tools/memory_tool.py` | 485 | the `memory` tool: schema, validation, approval gate, background-review delete gate, staged replay |
| `tools/memory_tool_store.py` | 700 | `MemoryStore`: files, matching, budgets, batch, drift guard, locking, prompt rendering |
| `agent/memory_manager.py` | 930 | fan-out to the builtin store plus one external provider; almost all of it is provider plumbing |
| `agent/memory_context.py` | 185 | `<memory-context>` fencing for *external* recall, and scrubbing those fences from output |
| `agent/memory_manager_tools.py` | 106 | exposing external provider tool schemas |
| `agent/memory_caller_scope.py` | first 40 | multi-profile scoping on a multiplexed gateway; skimmed, not relevant |
| `agent/prompt_builder.py` | memory parts | `build_memory_guidance`, `SESSION_SEARCH_GUIDANCE`, the "memory describes the user, not this machine" line |
| `agent/system_prompt.py` | memory parts | where the blocks sit in the prompt; reload on compaction |
| `agent/agent_init.py`, `turn_context.py`, `turn_finalizer.py` | memory parts | config, nudge counter, when the review fires |
| `agent/background_review.py` | prompts | `_MEMORY_ROUTING_BLOCK`, `_MEMORY_REVIEW_PROMPT`, `_COMBINED_REVIEW_PROMPT` |
| `hermes_cli/config_defaults.py` | `memory:` section | the defaults |
| `website/docs/user-guide/features/memory.md` | 555 | intended behavior, troubleshooting, write approval |
| `tools/session_search_tool.py` | about 450 of 806, plus the schema | discovery, scroll, read and browse shapes over FTS5 |
| `hermes_cli/default_soul.py`, `agent/context_file_sources.py` | skimmed | SOUL.md seeding and the `/context` manifest |

Skipped, and why:

- `agent/memory_provider.py`, `plugins/memory/*`, `memory-providers.md`, `memory-provider-plugin.md`, the Honcho skill and docs: external backends over network APIs, with the stuck-call gates, egress redaction and pre-compress checkpoints that only exist because of them.
- `hermes_cli/memory_*.py`, `web_server_memory.py`, `web_routers/memory_providers.py`, `gateway/memory_*.py`, `memory_oauth.py`, `memory_provider_migration.py`: setup UI, OAuth, web dashboard, migration.
- `agent/insights.py` (610): usage analytics for `/insights`, not memory. Checked by grep, not read in full.
- `evals/session_search_schema/`: Hermes's own evaluation of the tool schema.
- The `/journey` timeline and the `zh-Hans` docs.
- `hermes_state_search.py`: only the FTS query signature was read. Chelus has its own store.

## The built-in store

Two plain-text files in `~/.hermes/memories/`: `MEMORY.md` (the agent's notes on its environment, conventions, tool quirks) and `USER.md` (who the user is: name, role, preferences, style). Entries are joined by `\n§\n`, and an entry can span lines.

**Budgets are in characters**, so they don't depend on the model:

| Store | Limit | About |
|---|---|---|
| memory | 2,200 | 800 tokens, 8-15 entries |
| user | 1,375 | 500 tokens, 5-10 entries |

The budget counts the joined text, delimiters included. It is checked only on writes. A file that goes over its limit through a hand edit still loads, with a warning; nothing is truncated, but further adds fail until it's back under.

**Frozen snapshot.** At session start both files are loaded and rendered once into prompt blocks. Writes during the session go to disk immediately but don't change the prompt, which keeps the prefix cache. The snapshot is reloaded only when compaction rebuilds the prompt, since the cache is already broken then. The block looks like this:

```
══════════════════════════════════════════════
MEMORY (your personal notes) [67% — 1,474/2,200 chars]
══════════════════════════════════════════════
entry
§
entry
```

The header shows usage so the model knows when to consolidate. An empty store renders nothing.

**Load-time scan.** Each entry is scanned again at load. A hit is replaced *in the snapshot only* with a `[BLOCKED: ... use memory(action=remove) to delete the original]` placeholder, while the live list keeps the raw text so the poisoned entry stays visible and removable. Duplicates are dropped on load, keeping the first.

**Disk safety.** Every mutation runs under an exclusive lock on a separate `.lock` file (fcntl, or msvcrt on Windows) and re-reads the file inside the lock, so a CLI and a bridge writing at once don't drop each other's entries. Writes are temp file plus rename. Hermes refuses to write in two cases:

- **Unreadable file.** The file exists but can't be read or decoded (strict UTF-8, BOM stripped). Saving from an assumed-empty view would wipe it.
- **Drift.** Any single parsed entry is longer than the whole store's limit, which means someone appended free text without `§`. Rewriting would lose it, so Hermes saves a `.bak.<ts>` and refuses. An append-only `add` skips this check but still refuses on an unreadable file.

## The `memory` tool

One tool, three actions, two shapes. Every call needs a top-level `target` (`memory` or `user`). A call with neither `action` nor `operations` gets a specific error, because the generic one made models retry blindly.

- **`add`**: append an entry. Exact duplicates succeed with "no duplicate added".
- **`patch`**: replace one span (`old_text`) inside the entry it identifies with `content`, keeping the rest of the entry. The span must occur once in that entry, counting overlaps; a no-op patch is an error.
- **`remove`**: delete the whole entry that `old_text` identifies.
- **`replace` was removed.** It overwrote the whole matched entry, and models used it as find-and-swap, wiping the other facts in multi-fact entries. A call to it now errors and points to `patch`; to rewrite a whole entry, patch with the whole entry as `old_text`.
- **Batch**: `operations: [{action, content?, old_text?}, ...]` on one target, applied atomically, with the budget checked **only on the final result**. That is the consolidation path: remove or shorten stale entries and add the new one in one call. All-or-nothing; one bad op writes nothing. A batch that would empty a non-empty store is refused (single removes are the deliberate-wipe path). A nested op naming a different target is refused.

**Matching `old_text`**, in this order:

1. An entry exactly equal to `old_text` wins outright, so a short entry stays addressable even when a longer one contains it.
2. Otherwise, a plain substring.
3. Otherwise, a folded substring: all quotes and backticks become `'`, all dashes become `-`, a literal `\n` becomes a space, and whitespace runs collapse. Models retype entries with straight quotes, hyphens for em dashes and re-wrapped lines. A needle with no letters or digits never folds-matches.

Several distinct entries matching is an "ambiguous" error with previews (80 chars each). No match returns up to 3 `closest_entries` sharing at least 30% of the words, plus a hint when `old_text` contains `§` (it spans entries).

**Result shapes, deliberately:**

- **Success** is terminal and does *not* echo the entries: `{success, done, usage, entry_count, message, note: "Write saved. This update is complete — do not repeat it."}`. Echoing the list made the model "find more to fix". A patch, replace or remove does report the entry text it overwrote, so a lossy write is never silent.
- **An over-budget add** returns `current_entries` and tells the model exactly how many chars to free, as one batch.
- **A failed batch** does *not* echo `current_entries` (the model already has them; echoing grew the context on every retry).
- **Retry cap**: after 3 failed consolidation attempts in one turn (over budget or no match), the result turns terminal: stop retrying, leave memory alone, answer the user, save later. A success resets the count. "A failed memory side effect must never block the turn's reply."

**Content scan.** Adds, patches and batch contents go through the same threat scanner as skills, at its strictest scope, because memory enters the system prompt of every future session.

**The schema description is the main guidance.** Its WHEN, IF FULL, TARGETS and SKIP sections say: memory only for facts that apply to every session regardless of task; anything learned while doing a task goes in that task's skill; skip trivia, re-discoverable facts, dumps, task progress, completed-work logs and TODO state (use session search for those).

## Prompt guidance

The system prompt adds one paragraph (`build_memory_guidance`). Its points, in order:

- You have persistent memory, loaded into each new session.
- Skills come first for anything learned while doing a task.
- Memory is the narrow exception for facts that apply to every session, with a hard budget, so consolidate when full instead of skipping the save.
- **Write entries as declarative facts, not instructions.** "User prefers concise responses", not "Always respond concisely", because imperatives get re-read as directives in later sessions and can override the user's current request.
- A fact stale within a week belongs in session history.

Elsewhere it says "Your memory and user profile describe the USER, not the system you are running on." With session search available it adds: when the user references a past conversation, search before asking them to repeat themselves.

Prompt order: identity, guidance (static tier), context files, then the skills index, memory, user profile and timestamp (volatile tier).

## Writes from the background review

The review from [skills.md](skills.md) also covers memory. Memory has its own trigger: a counter of **user turns** (`memory.nudge_interval`, default 10), separate from the skills counter of tool iterations. It is restored from history on resume as `prior_user_turns % interval`. When either fires after the reply, one fork runs with the memory prompt, the skills prompt or the combined one.

The memory part of the review prompt, in substance: two stores, pick one per fact; `user` is persona, preferences, style and personal details; `memory` is the environment, tool quirks, conventions, paths; one fact goes to one store, never both; save it once; if nothing stands out, say "Nothing to save." The combined prompt adds that a user correction about how a task was done goes in that task's skill, not memory, with `USER.md` only for cross-cutting preferences no skill owns, and never both.

**Background review may only `add`.** A `patch` or `remove` from an unattended review (single or in a batch) is never applied. It is staged for approval, pinned to the exact entry it targets. A staged destructive write applies only if that exact entry still exists at approval time, so it can't hit a newer entry that happens to contain the same text.

## Approval

`memory.write_approval` is off by default: the agent writes freely, foreground and background. When on, CLI foreground writes prompt inline (entries are short enough to read whole), and everything else (gateways, the background review) is staged under `/memory pending | approve <id|all> | reject <id|all>`. Hermes presents this as the fix for "the agent saved a wrong assumption about me". Background deletes are staged even with the gate off.

## Session search

`session_search` is one tool with four shapes, chosen by its arguments, and no LLM calls:

- **Discovery** (`query`): FTS5 over user and assistant messages, top 3 sessions by default (max 10), deduplicated by session lineage. The top hit is hydrated in full (3-message bookends, plus a window of 5 around the match, messages capped at 4,000 chars and bookends at 1,200); the rest return only the matching message. Optional `sort` (newest or oldest as a bias over BM25), `after`/`before` (ISO or `7d`/`24h`/`2w`), and `exclude_session_ids` (cap 20) to explore past hits already seen.
- **Scroll** (`session_id` + `around_message_id`): a window around an anchor.
- **Read** (`session_id`): a whole session, head and tail, each message capped at 2,000 chars, after a single 74K-char tool result once blew up a request.
- **Browse** (no args): recent sessions.

The rules that matter: the current conversation's live messages are excluded, since they're already in context, but its compaction-archived rows are searchable, because compaction pushed them out. Compaction summaries are kept out of bookends. Cron sessions are demoted below interactive ones, since their repetitive vocabulary drowns BM25. A zero-hit search explains the FTS5 syntax (OR, quoted phrases, NOT, prefix `*`). The description says to check a direct source (file, URL, live system) before concluding "not found" from history.

## Defaults

```yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
  write_approval: false
  memory_char_limit: 2200
  user_char_limit: 1375
  nudge_interval: 10        # user turns between memory reviews
  provider: ""              # external backend; empty = built-in only
```

Turning both stores off removes the tool and its guidance. With one off, the schema lists only the other target.

## Chelus today (2026-10-24)

- **No cross-conversation memory at all.** History is per conversation, and compaction summarizes within one.
- **The parts memory would reuse already exist.** These are the skills pattern of a frozen prompt section stored on the session row and refreshed at compaction (`Agent._prepare_skills`), `skill_guard.scan`, the background review with pending cards and buttons, and the owner and guest split.
- **SQLite on the phone has FTS5.** It's version 3.54.1, and a `snippet()` test query works. Messages are stored as JSON in `messages.body`, and compacted rows are soft-archived, not deleted.
- **The owner's Telegram chat is one long conversation** (511 rows on the phone today). It's exactly the "never ends a session" case Hermes warns about, so the compaction-time reload is what keeps memory current there.

## Port list (proposed, for agreement)

1. **Store and `memory` tool.** Port `MEMORY.md` and `USER.md` under `~/.local/share/chelus/memories/`, `§` entries, char budgets 2,200 and 1,375. Bring add, patch and remove, the batch with a final-budget check, the three-step matching with folding, the error shapes (closest entries, terminal success, no echo on a failed batch), and the 3-failure cap per turn. Also the scan via `skill_guard`, the drift and unreadable-file refusals, fcntl locking and atomic writes. Keep the files hand-editable. Drop `replace` from the start. Owner only: guests get no tool and no memory in their prompt, since `USER.md` is the owner's profile.
2. **Prompt.** Render both blocks from a snapshot frozen on the session row, the same way as the skills index, refreshed when compaction runs. Add the guidance paragraph with the declarative-facts rule and the "memory describes the user, not this phone" line. Owner prompt only.
3. **Commands.** Add `/memory` in Telegram and `chelus memory` in the CLI: show both stores with usage, and pending changes. On Telegram, a one-line notice when Chelus saves something, so a claimed save that never happened is visible.
4. **Review.** Add memory to `review.py`: a user-turn counter (10), the combined prompt when both are due, `add` applied directly, `patch` and `remove` staged as pending cards pinned to the exact entry, through the same Approve and Reject buttons as skills.
5. **`session_search`.** An FTS5 table over the text of user and assistant rows, kept in sync on insert, with discovery and scroll shapes, compacted rows searchable, and live rows of the current conversation excluded. Owner only. It costs nothing in the prompt and is the only recall for anything older than the live window.

Decisions (2026-10-24): memory writes need no approval (the reply shows each one; background patches and removes still wait for the owner); no undo; session search built with the rest. All five items are built. Not ported: write_approval, the CLI inline prompt, external providers.
