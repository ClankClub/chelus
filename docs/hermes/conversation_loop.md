# Hermes `agent/conversation_loop.py` — reading notes

Source: NousResearch/hermes-agent @ 99fcd69 (2026-10-24), MIT. 1,936 lines.
Purpose (its docstring): `run_conversation(agent, ...)` drives ONE user turn: model call, tool dispatch, retries, fallbacks, compression, post-turn hooks.

## Lines 1–400: imports and helpers

**Structure.** The loop is split into phase modules, each imported at load time "so a source-tree swap cannot load a skewed phase mid-turn" (they update code while it runs):
`turn_preflight_gate` → `turn_iteration_prep` → `turn_request_assembly` / `turn_api_request` → `turn_api_call` → `turn_api_error` → `turn_response_check` → `turn_response_intake` → `turn_tool_round` → `turn_final_response` → `turn_finalizer`. Outer errors: `turn_loop_errors`. Failure wording: `turn_failure_copy`.
So conversation_loop.py is the conductor; most real work lives in `agent/turn_*.py`.

**Ideas worth knowing (with Chelus relevance):**
- *Run budget wrap-up* (`RUN_BUDGET_WRAPUP_NOTICE`): at 80% of a wall-clock budget, append a notice to the newest tool result telling the model to stop exploring and deliver. Appended to a tool message, not a new message, so the cached prefix stays byte-identical. **Chelus:** cheap and useful; our iteration cap just stops. Port candidate.
- *Prompt-cache discipline* recurs everywhere: never mutate messages already sent/persisted (`_DB_PERSISTED_MARKER`); only the live tail may change. DeepSeek also caches prefixes (cache-hit input is far cheaper), so this matters for us too.
- *Interrupt redirect* (`_apply_active_turn_redirect`): when the user corrects the agent mid-reply, add an assistant checkpoint (the visible partial text) + a user correction, keeping strict user/assistant alternation. Raw chain-of-thought is never replayed ("reads as a prefill jailbreak and bricks the session"). Runaway-repetition text is dropped. **Chelus:** we only cancel; the "keep alternation, never replay reasoning" rules apply to us.
- *Truncated-reply joining* (`_join_truncated_parts`, KMP overlap): when a reply hits max tokens and is continued, dedupe the overlapping seam. **Chelus:** we have no continuation; max_tokens 2048 just truncates. Possible port.
- *Local vs. API error classification*: exceptions from local processing code are deterministic bugs, not retried; only API-call failures are. Max 8 outer-loop errors per turn.
- *Interpreter-shutdown errors*: abandon quietly, no retry spam.
- Copilot, MoA (mixture-of-agents), Codex-native-compaction special cases: provider-specific, skip.
- Compression has an anti-thrash budget that only re-arms when the provider's REAL token count proves compaction worked (rough estimates can't re-arm it). Lesson: trust provider-reported usage over local estimates.

## Lines 400–800: more helpers (context pressure, billing, prompt/tool pinning)

- *Real token count beats estimates* (`_pressure_with_real_floor`): rough local token estimates undercount non-ASCII text up to ~2x; some providers silently clip instead of erroring, causing a "truncation death spiral." Rule: never let the estimate fall below the provider's last reported `prompt_tokens`. **Chelus:** we budget history by characters (`history_chars`), no provider usage at all. DeepSeek returns `usage.prompt_tokens`; we should record and use it. Port candidate (small).
- Ollama/llama.cpp local-model context checks: skip (no local models on a phone).
- *Billing errors get their own path*: a 402/credits failure is classified, marked non-retryable unless the classifier says otherwise, and the user gets one clear actionable message (billing link, "switch provider"). Notable honesty rule: when an error is only *probably* billing (Anthropic returns the same 400 for content-filter rejections), the message says "unverified" instead of asserting it. **Chelus:** our ProviderError already maps 402 to a "top up your balance" hint; good enough. The "don't assert what you can't verify" pattern is worth keeping.
- Failure results are structured dicts (`final_response`, `failed`, `failure_reason`, `failure_retryable`...) so every surface (CLI, Telegram, desktop) renders them the same way. **Chelus:** we raise exceptions and each door formats its own message. Fine at our size.
- *System prompt is persisted per session* and reused byte-for-byte so the provider's prefix cache keeps hitting; failure to persist is logged loudly because it silently breaks caching. **Chelus:** we rebuild the system prompt every turn, including environment info. If any of that changes per turn (time, battery), we lose DeepSeek's cache. Worth checking.
- *Tool list is pinned per session* for the same reason: changing the tools array changes the prefix and breaks the cache. Tools only change at deliberate moments.
- Bot Chat / desktop / MCP tool refresh: Hermes product surfaces; skip.

## Lines 800–1200: system-prompt reuse, recovery nudges, tool-call hygiene

**System prompt reuse** (`_restore_or_build_system_prompt`): first turn builds and saves the prompt; every later turn reuses the saved bytes exactly. It rebuilds only when the prompt's identity lines are stale: model, provider, session ID, or working directory changed (`_stored_prompt_matches_runtime`). A surface switch (TUI → desktop) is *not* a rebuild; a note is appended at the tail instead, so the cached prefix survives. **Chelus:** clear port target. Save the system prompt per session in `store.py`; rebuild only on model/workdir change. Saves money (DeepSeek cache hits) and time on a slow phone.

**Recovery nudges** — canned system messages for known model failure modes. These are distilled bug reports; most apply to any model:
- `_LENGTH_CONTINUATION_OUTPUT_LIMIT`: reply hit the output-token limit → "continue exactly where you left off, don't repeat." (Pairs with `_join_truncated_parts`.)
- `_LENGTH_CONTINUATION_NETWORK_STUB`: stream cut by network → continue; also "this was a transport problem, your tools still work" — models otherwise conclude they lost tool access.
- Dropped-tools variant: a giant tool call (e.g. writing a huge file) timed out → "break it into calls under ~8K tokens."
- `_DEGENERATE_FINAL_NUDGE`: the turn ended on a fragment after real tool work → "finish, or resend if that was complete." Built so a false positive costs one call, never the answer.
- `_DROPPED_TOOLCALL_NUDGE_CONTENT`: finish_reason says tool_calls but none arrived → "issue the call, don't narrate."
- `_EMPTY_TOOL_RESPONSE_NUDGE`: empty reply right after tool results → "process the results and continue." **Chelus:** we currently return an empty reply as-is. Port candidate.
- Codex-only nudges (reasoning-only reply, ack-only reply): skip unless we add Codex.

**Tool-call hygiene:**
- Before sending, tool-call argument JSON in history is canonicalized (compact, sorted keys) on a *copy*; malformed JSON is repaired, never written back to the saved transcript (`_clone_message_for_send`, #80498 — they once overwrote stored turns with "{}"). Canonical form keeps the prefix byte-stable for caching. **Chelus:** we send stored arguments verbatim; malformed JSON from the model would be resent forever. Cheap port.
- Unknown tool name → error listing the available tools so the model self-corrects. Empty tool name → terse error and NO catalog: an empty name means the model is echoing tool-call syntax it saw in data, and dumping the catalog feeds that loop. **Chelus:** our unknown-tool error doesn't list tools. Tiny port.
- Content-policy blocks are terminal, never retried (same prompt = same block).
- Compression lock held / transiently blocked → "deferred," a soft result, never "exhausted" (the gateway wipes the session on exhaustion; they learned that the hard way).

## Lines 1200–1600: failover, context engine, and the loop's skeleton

- Context overflow that compression can't fix → save the transcript, end the turn with a typed "context_overflow, not retryable" result. Don't keep resending a request the provider already said is too big. Close any dangling tool-result tail first, so the next user message doesn't follow a tool message (strict providers reject tool → user).
- *Provider failover*: when switching to a fallback provider mid-turn, the already-built request is reused, so its system message and prompt-cache markers are re-synced for the new provider. Skip for now (one provider), but note that failover exists.
- *Context engine hooks* (`select_context`, `on_turn_complete`): a plugin can choose which messages go into each request. Engines always receive deep copies so they can't corrupt saved history; anything invalid (including an empty list) fails open to the unmodified request. That's the seam their memory/compression plugins use.
- **The loop's state** (`_LoopState`): one dataclass holding every variable the turn threads through its phases. Phase helpers declare which fields they need by parameter name; `_run_phase` fills them in and copies results back. A neat pattern, but it exists because their loop has ~45 pieces of state. Chelus has about 5.
  Per-turn counters show what they guard against: `api_call_count`, `length_continue_retries`, `truncated_tool_call_retries`, `compression_attempts`, `restart_count` (a backstop so interrupt/redirect restarts can't refund the budget forever), `_outer_error_count`.
- **`_run_api_retry_loop`**: ONE model call with retries: rate-limit guard → build request → call → check response. Interrupts and errors go to handler phases that answer return (end the turn), break (leave the retry loop) or continue (retry). **Chelus:** we have no retries at all. A single network blip or 5xx fails the whole turn. Port target: a small retry with backoff for timeouts/429/5xx only, never for 4xx.

## Lines 1600–1936: the turn itself

**`_run_conversation_turn`** — the actual loop, and it's short once the phases are factored out:
```
setup: reset per-turn flags, refresh credentials from .env, build_turn_context (system prompt, history, user message)
if codex app-server mode: hand the whole turn to the codex subprocess
play scripted prelude (tool calls run before the first model call)
while api_calls < max_iterations and budget remains:
    begin_iteration → prepare_iteration → assemble_api_request
    preflight gate (compress if too big; may return/break/continue)
    announce
    _run_api_retry_loop  (one model call, with retries)
    apply_retry_restarts (fallback/redirect may restart the iteration)
    normalize_model_response
    if tool_calls: run_tool_round  else: finish_text_response
    outer exception → handle_outer_loop_error
finalize_turn (save, usage, hooks) → result dict
```
That's the same loop as Chelus's `run_turn`. The difference is all in what each phase does.

- Per-turn state is reset explicitly at the start, because the gateway reuses one agent object across turns: nothing may leak into the next message. **Chelus:** the Telegram bridge builds a fresh Agent per message, so we get this for free.
- `iteration_budget` + `_budget_grace_call`: when the budget runs out, the model gets one grace call to wrap up instead of being cut off mid-thought. **Chelus:** at the cap we append a note and stop without a final answer. Port candidate: one last call with tools disabled, asking for a summary.
- **`_close_durable_failed_turn`**: when a turn fails after the user message was saved (refusal, retries exhausted, interrupt), append an assistant "this turn failed" row so history never ends on a dangling user/tool message. Otherwise the next message produces two user rows in a row, and history repair merges the failed request into the new one. Excluded for context-overflow failures (adding rows to an oversized session feeds the growth loop). **Chelus:** we already do the equivalent (the "[this turn cancelled/failed …]" note). Same lesson, reached independently. Good sign.
- Error text that gets *saved* is force-redacted for secrets, because provider/tool errors can echo keys back. **Chelus:** our ProviderError and tool errors could in theory echo a key into history. Port candidate: run saved error strings through a redactor.

## Port list

Moved to [brain_summary.md](brain_summary.md), which covers this file and all the turn phases.
