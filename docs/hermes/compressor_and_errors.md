# Hermes `context_compressor.py` and `error_classifier.py`, reading notes

Source: NousResearch/hermes-agent @ 99fcd69, MIT. These are the two brain-adjacent files that items 11 and 12 of [brain_summary.md](brain_summary.md) depend on. `chat_completion_helpers.py` (4,014 lines) was not read: it builds requests for the provider zoo, and nothing Chelys ports needs it.

## context_compressor.py (5,744 lines)

Most of the file is defense against incidents in a multi-provider, multi-process, image-carrying, skill-loading agent: role-alternation repair for strict chat templates, image stripping, skill-marker re-injection, lease and staleness fencing for a background compression worker, and rehydrating handoffs after a restart. The core algorithm is small.

**Trigger.** Compress when the provider's real `prompt_tokens` passes `threshold_percent` (default **0.50**) of the usable input window, which is `context_length - max_tokens`. That value is floored at 64k (`MINIMUM_CONTEXT_LENGTH`) so large windows don't fire early. When the floor binds on a small window, it is capped at 85% of the window. Gates and refunds are in turn_preflight (see [turn_phases.md](turn_phases.md)).

**`compress(messages)`, four phases:**

1. **Prune old tool results** (no LLM). Each tool-result body older than the protected tail and longer than 200 chars becomes a one-line summary per tool, e.g. `[terminal] ran `cmd` -> exit 0, 42 lines output` or `[write_file] wrote to path (N lines)`. A failed write is summarized as failed, never as "wrote". Tool-call **arguments are never rewritten**. The pending tool round (results the model hasn't read yet) is never pruned.
2. **Pick the window.** Head = the system prompt plus `protect_first_n=3` rows. That head protection **decays to 0 after the first compaction**, so early turns don't fossilize. Tail = walk backward from the end, adding up tokens until the budget is reached. In lean mode the budget is `clamp(2.5% of context, 10k, 25k)` tokens, capped at 20% of context, with a 1.5x soft ceiling. Tail rules:
   - Never split a tool group (an assistant tool-call row plus its results). Boundaries are aligned backward onto the parent assistant row.
   - The last user message and the last text-bearing assistant reply always stay in the tail. Exception: when the active turn alone is over the ceiling, the cut stays at a tool-group boundary and the user request is restated after the summary.
   - The newest row always stays.
   - At least 2 non-head messages must be summarizable, or compression is a no-op.
3. **Summarize the middle** with one LLM call. The summary budget is `max(2000, min(20% of the middle's tokens, min(5% of context, 10000)))` tokens. Input is serialized as `[USER]: …`, `[ASSISTANT]: …` plus `[Tool calls: name(args)]`, and `[TOOL RESULT id]: …`. Each body is capped at 6,000 chars (keep the first 4,000 and the last 1,500), tool arguments at 1,500 chars, and the total at 160k chars. Secrets are redacted on input and on output.
   - The prompt says the turns are data, never instructions, and asks for a fixed set of sections: *Historical Task Snapshot* (the user's latest unfulfilled input, "THE SINGLE MOST IMPORTANT FIELD"), Goal, Constraints & Preferences (security constraints quoted verbatim), Completed Actions (numbered, `N. ACTION target — outcome [tool: name]`), Active State, Blocked, Key Decisions, Errors & Fixes, Resolved Questions (with answers), Relevant Files, Critical Context.
   - It adds a temporal-anchoring rule: write finished actions as dated past-tense facts.
   - The summary is written in the user's language.
   - **Iterative update:** after the first compaction, the previous summary is passed in with "PRESERVE all existing information that is still relevant, ADD new completed actions (continue numbering), move resolved items".
   - **On failure:** retry once on the main model, then fall back to a deterministic summary built from the user asks, tool actions, file paths and errors found in the middle, capped at 8,000 chars. Network, quota, empty and truncated failures abort instead, leaving history unchanged. A failure cooldown applies: 600 s, with a timeout ladder of 60/300/900 s.
4. **Assemble** the head, then the summary, then the tail. The summary is prefixed with `SUMMARY_PREFIX` and suffixed with an end marker (`--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---`).
   - The prefix is a long "REFERENCE ONLY" handoff: don't answer requests in it, respond only to the latest message after it, and do nothing if there is none. A Jul-2026 incident added the clause "your tools remain fully active", after the framing suppressed tool use for 7 turns.
   - The summary's role is chosen so roles alternate. If neither role fits, it is merged into the first tail row.
   - A compaction note is appended to the system prompt once. That is a one-time cache break, accepted.
   - Orphaned tool pairs are then sanitized.

**Persistence** (`archive_and_compact`). In one transaction, under the same session id, the active rows are soft-archived (`active=0, compacted=1`, still searchable) and the compacted list is inserted as fresh active rows. A watermark protects rows that arrived during the slow summary call: they are re-sequenced after the new set rather than lost.

**Cache rule.** Every compaction breaks the prompt-cache prefix. Micro-compaction is off by default for that reason, and prunes are committed only when they reclaim enough tokens (≥ 4,096) to be worth the break.

**For Chelys:** keep phases 1–4 with the same numbers, scaled to DeepSeek's window, and store with soft-archive. Drop the multi-process machinery, images, skills, role-alternation repair (DeepSeek accepts user-after-tool) and the background worker. Compaction runs inside the turn, before the call that needs it.

## error_classifier.py (1,577 lines)

This is a pipeline of stages that maps an exception to a `FailoverReason` plus hints: `retryable`, `should_compress`, `should_rotate_credential`, `should_fallback`. Stage order: plugin, provider profile, provider special cases, HTTP status, structured error code, message patterns, transport (SSL, disconnect, exception type). Anything unmatched is `unknown`, retried with backoff. Most of the 1,577 lines are per-provider patterns (OpenRouter upstream 429s, Codex replay blobs, Anthropic tiers, Z.AI using 429 for overload). DeepSeek has no special case.

The rules that apply to a single provider:

- **Transient:** 500/502 (server_error), 503/529 (overloaded), 429 (rate_limit, unless the body says quota or billing, which counts as billing and is not transient), timeouts, and transport errors (`ConnectionError`, `TimeoutError`, `OSError`).
- **SSL:** a certificate verification failure (`certificate verify failed`, `self-signed`, `expired`, …) is **deterministic: fail fast with guidance, never retry**. Transient SSL alerts retry.
- **Server disconnect** (`connection reset by peer`, `unexpected eof`, `incomplete chunked read`, …) with no status code and a large session is treated as probable context overflow. On reasoning models it is reclassified as a timeout.
- **400:** overflow if the message says `context length`, `maximum context`, `token limit`, `too many tokens`, … (should compress). A generic bare 400 on a large session is also treated as probable overflow. Otherwise format_error (abort, don't retry unchanged). 5xx bodies that are really validation errors also count as format_error.
- **401/403** auth: abort. **402** billing: abort. **404** model_not_found: abort.

**For Chelys:** `provider.py` already retries the right set. Two gaps:
- Certificate-verification failures are `OSError`s, so Chelys currently retries them 3 times for nothing.
- A context-overflow 400 should trigger compaction rather than fail.

Autorecovery (item 11) applies to exactly server_error, overloaded and timeout, per turn_recovery_autorecover.
