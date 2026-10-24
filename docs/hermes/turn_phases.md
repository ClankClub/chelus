# Hermes `agent/turn_*.py` — the loop's phases, reading notes

Source: NousResearch/hermes-agent @ 99fcd69, MIT. 33 files, ~13,000 lines. Read in the order a turn runs.
See [conversation_loop.md](conversation_loop.md) for the conductor.

The "Chelys today" notes describe Chelys before the port: atomic turns, no compaction, no lease. For what Chelys does now, see [brain_summary.md](brain_summary.md).

## turn_iteration_prep.py (566 lines) — top of each iteration

**begin_iteration**: apply a pending mid-turn correction; stop if interrupted; count the call against the iteration budget. When the budget is exhausted, the model gets one **toolless grace call** (`_budget_grace_call`). An optional warning was considered and rejected as an extension: "the warning never extends the hard budget."

**prepare_iteration** (every mutation "cache-safe by construction"):
- `/steer` text typed during a model call goes in as a *new user row after the newest tool result*, never edited into the already-saved tool row.
- Optional iteration-budget warning ("you have used N of M iterations; checkpoint progress, then continue; do not stop solely because of this warning"), appended to the newest unsaved tool result.
- Tool-call arguments are sanitized every iteration, with a cursor so already-checked calls aren't re-parsed.
- **Role-alternation repair before every request**: tool→user or user→user tails get fixed, because "providers return empty content on them and the empty-retry loop spins." **Chelys:** our history can end in user→user after a failed turn plus a new message (we add a note, so mostly OK). A cheap repair pass before sending is worth porting.
- Hidden interrupt placeholders whose wording the model starts echoing are neutralised on a copy, never deleted (deleting can create tool→user).

**announce_api_call**: prints request size or a spinner. UI only.

**apply_retry_restarts**, after the retry loop, consumes restart flags in priority order:
- mid-turn redirect → redo the iteration and *refund* the budget, but at most `max_retries` restarts since the last response, otherwise a runaway interrupt could hold the turn forever;
- compression restart → refund, but count toward retries so "a compression that shrinks messages but not enough can't loop forever";
- fallback-provider restart → refund, bounded the same way;
- length continuation → raise the output-token cap on a ladder and retry;
- no response after all retries → save, tell the user "/retry or /model," end the turn.
Lesson: **every refund/restart path has a bound.** The restart counter resets whenever a real response arrives (counting for the whole turn wrongly killed healthy interactive turns, #128000).

## turn_request_assembly.py (286 lines) — building the request

Order is "load-bearing"; each step works on a *copy* (`api_messages`), never on saved history:
1. build messages from the transcript + system prompt (`turn_context.build_api_messages`);
2. optional mixture-of-agents context; ephemeral prefill messages right after the system prompt;
3. context-engine selection hook;
4. `_sanitize_api_messages`: **drop orphaned tool results** (a tool row with no matching call, e.g. from a cut history) — always on;
5. evict stale images;
6. drop thinking-only assistant turns and merge adjacent user messages (Anthropic rejects a trailing thinking block);
7. **strip whitespace from every string content and canonicalize tool-call JSON → byte-identical prefixes across turns**;
8. strip lone UTF-16 surrogates (some models emit them; they crash json.dumps);
9. build prompt-cache markers LAST;
10. estimate tokens, floored at real provider usage.

**Chelys relevance:**
- Orphan sweep: our `recent_window` always starts at a user message, so we never cut between a call and its result. A sweep is still cheap insurance; we already avoid the main way orphans happen.
- Whitespace + JSON canonicalization on the copy: tiny, helps DeepSeek's prefix cache. Port.
- Surrogate stripping: our json.dumps would raise on a lone surrogate the model sent back. Port the 3-line sanitizer.
- The rest (MoA, images, Anthropic cache markers) doesn't apply.

## turn_preflight_gate.py (102 lines) — "is the request too big?"

Before each call, compare estimated request size against the compression threshold and compress if needed (the work happens in `turn_preflight.py`). Key safeguard: if a compression pass made **insufficient progress**, stop trying this turn instead of compressing again and again. Compares fully assembled requests, not raw history. **Chelys:** no compression yet; we cut old history by characters. When we add compression, copy the "insufficient progress → stop" rule.

## turn_api_request.py (185 lines) — finishing the request for this attempt

Rebuilt per *attempt*, because a fallback provider may have different rules. Notable lines:
- `_reapply_reasoning_echo_for_provider`: "a fallback (DeepSeek / Kimi / MiMo) may require reasoning_content — re-apply the echo-back pad." **The same DeepSeek rule Chelys already follows** (provider.py pads with " ").
- `sanitize_outbound_kwargs`: scrub the whole payload, not just messages (tool descriptions, extra fields).
- OpenRouter caches identical responses *including empty ones*, so an empty-response retry sends a no-cache header. Lesson: a retry that sends the identical request can get the identical bad answer.
- Request middleware and a `pre_api_request` hook for plugins/observability; `HERMES_DUMP_REQUESTS` env var dumps the payload for debugging. **Chelys:** a `CHELYS_DUMP_REQUESTS` switch would make debugging on the phone much easier. Small port.

## turn_api_call.py (293 lines) — making the call

- **Streaming is preferred even with no display**, for *liveness*: a streaming connection shows progress, so stalls are detected early instead of waiting out a long timeout. **Chelys:** non-streaming with a 60 s timeout. Streaming would also let Telegram show text sooner. Medium port; noted.
- Calls are interruptible (`_interruptible_api_call`), run inside a middleware wrapper.
- A correction typed while the model is answering can "cross" the response: the stale response is discarded and the turn rebuilt from the correction.
- **On interrupt, keep the partial text** as an assistant message marked `interrupted`, so the next turn knows what was half-said. Runaway-repetition partials are replaced with a hidden placeholder ("looped bytes there re-seed the loop"). **Chelys:** on Ctrl-C we keep the user message and a note, but drop any partial text (we have none without streaming).
- Nous Portal rate-limit guard: Nous-specific, skip.

## turn_api_error.py (439 lines) — when the call fails

Order of recovery:
1. try cheap fixes *before* classifying (e.g. bad characters);
2. process shutting down → abandon quietly;
3. **classify the error** (`error_classifier.classify_api_error`) → reason + flags: retryable, should_compress, should_rotate_credential, should_fallback;
4. try fixes *after* classifying (rotate key, shrink image, drop the reasoning setting…);
5. interrupted during error handling → stop (or rebuild from a correction);
6. route by class (rate limit, overflow → compress and retry, …);
7. **client errors (most 4xx, local bugs) are not retried**: try a fallback provider, else end the turn with a clear message. **402 billing is deliberately non-retryable**: "retrying only burns paid requests on a depleted balance." Same for 401/403;
8. retries exhausted → rebuild the HTTP client once (stale connection, TCP reset) → fallback provider → a bounded "auto-recovery ladder" for outages → terminal result;
9. otherwise **back off and retry**, and the wait itself is interruptible.

`_is_local_validation_error`: ValueError/TypeError mean our bug, not the network, EXCEPT JSON decode errors, stream-parse errors and SSL errors (SSLError is both an OSError and a ValueError; a TLS failure isn't our bug). Those are retried.

**Chelys relevance (the most important file so far):** Chelys has none of this. One failure = turn over. Minimum viable port:
- classify: timeout / connection reset / 429 / 5xx / JSON-decode → retryable; 400/401/402/403/404/422 → not;
- retry up to 3 times with growing backoff, honoring `Retry-After` on 429;
- Ctrl-C must still work during the wait;
- keep the existing friendly messages for non-retryable errors.
That's maybe 40 lines in provider.py.

## turn_response_check.py (352 lines) — "is this response usable?"

After a successful HTTP call:
1. **Validate the shape** (`validate_response_shape`): missing choices, empty/malformed body → treated like an error. "Empty responses often mean rate limiting," so try a fallback eagerly, else retry with **jittered backoff (5 s base, 120 s max)**, interruptible. **Chelys (corrected after reading provider.py):** a 200 with no choices does *not* crash us. `provider.py` catches KeyError/IndexError and raises `ProviderError("unexpected response…")`, so the turn ends with a saved note. What we lack is the *retry*: Hermes treats it like a transient server error and backs off.
2. Derive `finish_reason`. Some local models say "stop" when they were actually cut off; Hermes detects suspicious stops and treats them as truncation.
3. `content_filter` → a refusal is deterministic; one fallback try, otherwise return the refusal.
4. `length` → truncation recovery (`turn_truncation.recover_from_truncation`).
5. **Record usage** (`turn_usage.record_response_usage`): tokens feed the compressor, session counters and the database.
Note: "bytes back != usable content", so the retry buffer isn't cleared just because HTTP succeeded.

## turn_response_intake.py (197 lines) — turning the response into a message

- Normalize to an assistant message whose `content` is always a string: some servers return content as a dict or list, which crashes `.strip()`. **Chelys:** DeepSeek returns strings, but a cheap `str()` guard costs nothing.
- Post-request hook for observability.
- Relay a first-line "thinking" preview to the progress display (subagents → parent).
- **Unclosed reasoning scratchpad** (`<REASONING_SCRATCHPAD>` opened, never closed) = ran out of tokens mid-thought → retry up to 2 times, then save as partial. Models that inline `<think>` tags have the same issue. **Chelys:** DeepSeek keeps reasoning in a separate field; skip.
- Codex "incomplete" continuation: skip.

## turn_tool_round.py (313 lines) — running a batch of tool calls

Order:
1. `validate_tool_calls` (unknown names, bad JSON, truncated calls; see turn_tool_validation.py);
2. dedupe identical calls in one batch; cap how many subagents can be spawned at once;
3. **mixed batch** (some valid, some unknown): the assistant message keeps *every* call, because each call needs a matching result, but invalid ones get an error result and aren't run;
4. **persist the tool-call message BEFORE running anything**: "resume must see the executed block if a destructive tool restarts Hermes." If saving fails, *don't run the tools*; end the turn;
5. execute (`agent._execute_tool_calls`, in tool_executor.py, not a phase file);
6. if a tool result couldn't be saved, end the turn rather than show the model unsaved state;
7. guardrail halt (a tool policy stopped the run) → explain it as a normal message, not a crash;
8. compress if tool results made the context too big;
9. "touch activity" so the gateway's inactivity watchdog (default 1800 s) doesn't kill a long turn.
Bits: a bare bracketed token like `[memory]` next to a tool call is protocol junk and gets dropped, otherwise it replays forever. Text the model writes *alongside* tool calls is kept as a fallback answer in case the post-tool reply comes back empty.

**Chelys relevance — a real design difference:** Chelys saves the whole turn in one transaction *at the end* (`store.append_turn`). If the phone dies mid-turn after `write_file` ran, history has no record that the file was written. On Ctrl-C/API failure we do note which tools ran, but not on a hard kill (Android OOM-killer, battery death), which is exactly what a phone does. Hermes's rule fits us *better* than it fits a laptop. **Port candidate: persist the assistant tool-call message (and each result) as it happens.** That replaces the "atomic turn" design, so it's a deliberate decision, not a quick patch.
Also port: keep text-alongside-tool-calls as a fallback answer; dedupe identical calls; mixed-batch handling (we error per call already, so mostly fine).

## turn_final_response.py (376 lines) — the model answered in plain text

Guards, in order, before accepting the text as the final answer:
1. **Reasoning-only clean stop**: some backends file the whole answer under "reasoning." On trusted routes it's promoted to the answer, but never stored as ordinary content.
2. **Empty / think-only reply** → `turn_empty_response.recover_empty_response` (its own file).
3. **Stall guard**: the reply ends with an announced next step ("Let me check the file…") but makes no tool call → re-prompt "Continue now. Execute the required tool calls…", **max 2 per turn**. **Chelys:** DeepSeek does this sometimes. Port candidate, high value, small.
4. **Degenerate final**: after real tool work the turn ends on a short fragment → `_DEGENERATE_FINAL_NUDGE`, same counter, max 2.
5. Join truncated continuation parts.
6. **Runaway repetition**: a long reply that's the same text looping is discarded with an explanation instead of delivered.
7. **Dropped tool call**: finish_reason says tool_calls but none arrived → re-prompt, max 3 in a row. Both the bad message and the nudge are marked ephemeral and never saved.
8. Pop all ephemeral scaffolding rows (nudges, prefills) so they never become durable history.
9. Stop gates (verification, kanban) → maybe continue.
10. Plugin output transform *before* saving, so what's stored matches what the user saw.
11. Append the final message and flush to the DB; a flush failure doesn't abort (the finalizer retries).

**Design lesson:** every nudge is **bounded** (2 or 3) and **ephemeral** (never saved). That keeps recovery loops from becoming infinite and keeps history clean. Chelys should adopt both rules for any nudge it adds.

## turn_empty_response.py (296 lines) — the model said nothing

A ladder. The order matters, and each rung runs only if the one above didn't apply:
1. **Partial stream**: if streaming delivered real text before the connection died, use that text as the answer.
2. **Reuse earlier content**: if the previous step had text *plus only housekeeping tools* (memory saves and the like), the model was already done, so that text becomes the answer. With substantive tools that text was mid-task narration, so this rung doesn't apply.
3. **Post-tool nudge, once per turn**: append `assistant("(empty)")` and then a user nudge. The fake assistant row exists only because APIs reject tool→user with nothing between. Both rows are flagged synthetic.
4. **Thinking-only prefill, max 2**: the model thought but wrote nothing, so append its reasoning as a partial assistant message and call again. It sees its own thoughts and finishes.
5. **Budgeted retries** with jittered backoff (5 s base, 60 s max). Each empty retry *re-bills the whole input*, so Hermes records each empty attempt's signature, and if empties look deterministic (same input, same nothing) it stops paying for retries. The budget shrinks for expensive requests.
6. **Fallback provider**, if one is configured; refunds the iteration.
7. **Terminal `(empty)`**: the saved row holds the sentinel `(empty)`, never the reasoning, so a later "continue" doesn't replay it and loop.

**Chelys today:** `agent.py` returns `msg.get("content") or "(no reply)"`. No nudge, no retry. With thinking off on DeepSeek, rungs 1, 4 and 6 don't apply. **Port candidate: rung 3**, a one-shot nudge when an empty reply follows tool results. That's the common DeepSeek failure. Maybe rung 5 with a budget of 1 or 2. It's a few lines.
**Watch out:** if the nudge rows are kept they pollute history. Hermes marks them synthetic; Chelys would just not save them, which is easy because our turn list is ours to filter.

## turn_tool_validation.py (237 lines) — checking tool calls before running them

1. **Uniquify tool-call ids** against every id already used in the session. Some providers name every call `call_0`, and the sanitizer keeps only the first call/result per id, so duplicates silently eat results.
2. **Auto-repair names** (`_repair_tool_call`: fuzzy match a hallucinated name to a real one, e.g. `readfile` → `read_file`).
3. Unknown names:
   - **Mixed batch** (some valid): error only the bad ones; run the rest; strikes reset.
   - **All invalid**: append the assistant message plus an error result per call, then call the model again. **3 strikes** in a row → stop the turn as "partial." The error text lists the valid tools (`_invalid_tool_name_error_content`, in conversation_loop.py).
4. Arguments: dict/list → dumped to a JSON string; empty string → `{}`; non-string → `str()`.
5. Bad JSON:
   - If the args don't end in `}` or `]`, they were **cut off mid-stream**. Refuse to run and stop the turn. Routers sometimes rewrite finish_reason `length` → `tool_calls`, so you can't trust the finish reason alone.
   - Otherwise, the first 2 times: **silently re-request** with nothing appended, which works because sampling is random. The third time: append error results and let the model fix them.
6. Every exit path keeps role alternation valid: an invalid batch is always answered with *tool* rows, never a user message.

**Chelys today** (`agent._tool_result`): unknown name → `error: no tool named 'x'` (doesn't list valid tools); bad JSON or a non-object → `error: bad arguments…`; both are per call and the batch keeps going, so we already do mixed batches. `max_iterations=8` caps the loop, so there's no infinite strike problem.
**Port candidates, all tiny:** list valid tool names in the unknown-tool error; treat `""` args as `{}` (we already default `None` to `"{}"` but not `""`); **detect truncated args** (no closing brace) and say "your arguments were cut off, write less" instead of a generic JSON error. That last one matters with `max_tokens=2048` and `write_file`: a long file write is exactly how args get cut off. Name repair and id uniquifying: skip for now, since DeepSeek gives unique ids and real names.

## turn_retry_state.py (60 lines) — the "already tried that" flags

A dataclass, **created fresh for every API call**, with one boolean per recovery trick (refresh OAuth once, shrink images once, retry 429 once, clamp max_tokens after a 402 once…) plus "restart" signals the outer loop reads (`restart_with_compressed_messages`, `restart_with_length_continuation`, …).
**The pattern is the lesson:** every recovery fires **at most once per attempt**, so recoveries can't loop on each other. Most of the flags are provider auth Chelys doesn't have. If Chelys grows more than ~2 recoveries, copy the idea as a small dataclass rather than scattering `tried_x = False` locals.

## turn_loop_errors.py (171 lines) — an exception escaped the turn

`handle_outer_loop_error`, the catch-all for anything that wasn't already classified:
1. **Count every exception first** so permanent failures end even with an unlimited budget.
2. **Interpreter shutdown** (`sys.is_finalizing()`): persist best-effort, end the turn, tell the user how to resume. Never retry.
3. **Local bug vs API failure**: walk the traceback's frames and collect module names. If it went through local post-processing modules and never through the API-call modules, it's a **deterministic bug in our own code**: retrying gives the same crash, so stop now. Clever and cheap.
4. **Answer orphaned tool calls**: if the tail is an assistant message with tool_calls missing results, append `Error executing tool: …` for each. Otherwise the next request is malformed (every call needs a result) and the session is bricked.
5. **No synthetic assistant message** for non-tool errors, because it would pollute history and could create assistant→assistant.
6. Stop when: local bug, near max_iterations, or error count ≥ `min(_MAX_OUTER_LOOP_ERRORS, max_iterations)`. Otherwise "fallthrough" retries the iteration.

**Chelys today:** an unexpected exception in `run_turn` propagates; cli/telegram catch it, and `_save_aborted` saves user message + note. Because we save atomically at the end, we can't have orphaned tool calls in the DB, which is the upside of our design. If Chelys switches to persist-as-you-go (see turn_tool_round), **step 4 becomes mandatory**: on load or on error, any assistant tool_call without a result must get a synthetic error result. Hermes calls this "closing the tool sequence."
Traceback classification: neat, but Chelys has one module that calls the API. Skip.

## turn_truncation.py (770 lines) — the reply hit a limit (`finish_reason == "length"`)

`recover_from_truncation` runs in this order, and the order matters:
1. **Runaway repetition was cut mid-stream** → end the turn and discard the partial. Continuing would re-enter the loop.
2. **Prompt filled the context window** (fewer than 512 tokens of room left, read from `usage.prompt_tokens` vs the window): don't continue, because each continuation *adds* ~100 tokens and makes it worse. Say "context window full" instead of "output limit."
3. **Stream ended on a context-overflow error** after a partial → end the turn, mark the session "compression exhausted" so the gateway starts fresh next time.
4. **Abort cases** (`_abort_reason`): thinking ate the whole budget (think tags, nothing after) → "Thinking Budget Exhausted, lower reasoning effort"; or the visible text is **repetition-dominated** (`repetition_guard.is_repetition_dominated`) → discard it.
5. **Content filter stall** → fallback provider; roll back the partial so the fallback starts clean.
6. **Text cut off (no tool calls)** → `_continue_text`: save the fragment (flagged `_length_continuation_fragment`), append a user nudge (flagged `_length_continuation_nudge`): *"[System: Your previous response was truncated by the output length limit. Continue exactly where you left off. Do not restart or repeat prior text. Finish the answer directly.]"* Up to **4 times**. Then `collapse_continuation_trail` **deletes all the fragment and nudge rows** and appends one clean assistant row of the joined text. Without that collapse, "unanswered continue nudges made every later turn re-truncate."
   - If a fragment was thinking-only, the next attempt goes with reasoning **off** (one-shot), otherwise it re-burns the same budget.
   - Never append an assistant row with no visible content: strict providers 400 on it.
7. **Tool call cut off** → `_retry_truncated_tool_call`: **don't append the broken response at all**; re-send the same request with a **boosted max_tokens** (`boosted_output_cap`: base·2ⁿ, at most max(32768, 2×cap), never above the model's known limit). Up to 4 times, then **refuse to execute** and end the turn. A cut-off tool call is never run.
8. Otherwise roll back to the last complete assistant message.

Joining fragments (`_join_truncated_parts`, conversation_loop.py): add a newline between parts if neither side has whitespace; dedupe overlap only at **network-stub** seams (an interrupted stream may resend text), not output-limit seams.

Also in this file: Codex `status=incomplete` continuation (max 3, reasoning-only streak → fallback). Skip, we're not on Codex. And `handle_content_policy_refusal`: a 200 with `finish_reason=content_filter` is **deterministic, never retried**. Try the fallback once, else show the refusal text (looking in reasoning or stop_details if the content is empty).

**Chelys today:** text cut off → we deliver it with `[reply cut off: hit max_tokens]` appended. **Tool call cut off → we try to run it anyway.** In practice a cut almost always lands inside the arguments string, so `json.loads` fails and the model gets `error: bad arguments`: a wasted step and a misleading error ("bad JSON" instead of "you wrote too much"), not a half-written file. A half file would need the provider to auto-close truncated JSON, which I have not seen DeepSeek do (unverified). *(Corrected: an earlier version of this note called it a half-file hazard; that was overstated.)* The fix is still worth it: never run calls from a `length` response, retry once with a bigger `max_tokens`, then tell the model to write less (e.g. split the file).
**Port, in priority order:**
1. finish=length **with tool calls** → don't run them; re-request with a higher max_tokens (we can just double once, up to DeepSeek's limit), else tell the user it was cut off. Small, important.
2. Text continuation: up to 2–4 nudges, then collapse fragments into one saved message. Medium. Our "atomic turn" design makes collapse easy: just don't save the scaffolding rows.
3. Repetition check before continuing. Port `repetition_guard.is_repetition_dominated` (need to read it).
4. Content filter: DeepSeek does return `content_filter` on some topics. Check it's not retried and say so plainly. Tiny.

## repetition_guard.py (194 lines) — "is the model stuck in a loop?"

Not a turn phase but the brain calls it from three places. **Pure stdlib (`math`, `collections.Counter`): liftable as-is under MIT.**
- `is_repetition_dominated(text)`: under 400 chars → False (fail-open). Otherwise True if any of:
  - **one stripped line** repeats ≥5 times and those copies cover ≥50% of the text (the fast, common case);
  - **one 60-char window** recurs often enough to cover half (catches loops with a changing counter);
  - **periodic run**: sample ≤32 anchors, find each anchor's next occurrence to get a period, expand `text[i]==text[i+period]` both ways, and check the run is ≥5 periods long and ≥50% of the text (catches long multi-line loop units).
- `is_runaway_repetition`: stricter. It also requires that ≤50% of the non-empty lines are distinct, so legit batch output (distinct INSERT rows sharing prefixes) isn't killed.
- On a *completed* ("stop") reply it only fires above **16,000 chars**: a user who asked for "say X 50 times" must get it.
- `RunawayStreamWatch`: the same check run on a live stream at 16k, 32k, 64k… chars, on the tail only, so cost stays linear.
- `REPETITION_LOOP_INTERRUPTED`: what gets saved *instead of* the looped text, because replaying the loop bytes re-seeds the loop next turn.

**Chelys:** with `max_tokens=2048` (~8k chars) a single reply can't reach the 16k stop-path threshold, so the stop-path check would never fire. It matters only once we add continuation (truncation item 2), where it guards the "continue" nudge. Port it alongside continuation, not before. Cost on the phone: one pass over ≤8k chars, negligible.

## turn_finalizer.py (812 lines) — after the loop, before returning

`finalize_turn` runs once per turn whatever happened. In order:
1. **Budget ran out with no answer** → `_handle_max_iterations`: **one extra request with tools off**, asking the model to summarize what it did. (If a verification gate was holding back an answer, deliver that instead of paying for another call.)
2. **"Silent stop"**: loop ended on a raw `tool` row with no text → mark failed and say *"No reply: the turn stopped while a tool result was still pending. Send `continue`…"*. Never leave the user staring at nothing.
3. Exit reason → failure verdict (`exit_reason_failure`) so the UI knows retryable vs not.
4. Each cleanup step (trajectory save, task cleanup, persist) runs inside `_guarded_cleanup`: **a cleanup failure is recorded, never allowed to lose the response.**
5. `_persist_step`, strict order:
   - drop scaffolding (empty-recovery nudges, verification nudges) so "continue" next turn doesn't replay them;
   - recover the final text from the stream if the last completion came back blank but text was already shown;
   - apply the output transform *before* writing, so stored = shown;
   - `_close_transcript_tail`: invariant **"a delivered final_response ⇒ an assistant row in the transcript."** Otherwise the next turn sees an unanswered user message and the model **re-answers it**. If the tail is a blank tool-call row, fill it in rather than adding a second row;
   - micro-compaction (fold the oldest exchange into a rolling summary), then persist.
6. `_log_turn_exit`: one INFO line per turn, *always*: `reason, model, api_calls, tool_turns, last_msg_role, response_len`. A WARNING if the last role is `tool` ("agent may appear stuck").
7. **File-mutation footer**: if a `write_file`/`patch` failed this turn and wasn't later fixed on the same path, append a footer saying so. That makes *"model claims it edited files it didn't"* structurally impossible.
8. `_explain_abnormal_exit`: if the reply is empty, `(empty)`, or a short (≤24 chars) fragment without closing punctuation, replace or extend it with a plain explanation of why the turn ended.
9. **Scrub lone UTF-16 surrogates** from the final text where it leaves the loop. They crash stdout, Telegram's length check, and JSON encoders.
10. Build the result dict (tokens, cost, flags), hand leftover `/steer` text back as the next user turn, clear interrupt state.
11. *After* delivery: external memory sync, background memory/skill review (a forked agent, ~30K tokens, skipped for cron), `on_session_end` hook.

**Chelys today:** at the iteration cap we return `[stopped: hit the limit of 8 model steps…]` with **no summary call**. We never end on a raw tool row, because the loop only returns on text or the cap. Saving is one atomic `append_turn`, so there's no partial-persist trouble. There's no per-turn log line.
**Port candidates:**
- **Grace/summary call at the cap** (already on the shortlist): one request with `tools` omitted. Small; a real UX win on 8 steps.
- **One log line per turn** (exit reason, steps, tools, reply length, maybe latency/tokens) to `~/.local/share/chelys/chelys.log`. Tiny, and it's what we'll want when the bridge runs overnight.
- **Failed-write footer**: tiny, and honest. Track `write_file` errors per path in the turn; if any survive, append a line.
- **Surrogate scrub** on the final text: one line (`s.encode("utf-16", "surrogatepass")`-style or a regex on `[\ud800-\udfff]`). Telegram is our main surface.
- Explain-abnormal-exit: we already write explicit notes; fine as is.
- Background memory/skill review: a later, separate feature (memory). Not part of the brain port.

## turn_stop_gates.py (220 lines) — "are you sure you're done?"

When the model gives a final text answer, four gates can turn it into an *interim* answer plus a user-role nudge and keep the turn going: late language-server errors on files edited this turn (max 3); verify-on-stop (run checks after edits); a `pre_verify` plugin hook; kanban workers that must end with a terminal board tool. The candidate answer is saved as `pending_verification_response`, so if the budget runs out it's delivered instead of nothing. The nudge rows carry a synthetic flag and get stripped by the finalizer, except the LSP note, which is a durable row (stripping it would change bytes already sent and break prompt caching).
**Chelys:** none of these exist (no LSP, no code-edit verification, no kanban). The **pattern** is the takeaway: a gate keeps the candidate answer as a fallback, so a gate can never make a turn end worse than it would have without it. Revisit if Chelys ever does coding work.

## turn_summary.py (209 lines) — the one-line "what this turn did"

Display only, pure functions. `⋯ 12.4s · edited 2 files +18 -3 · read 4 files · ran 3 commands`. Tool name → (verb, noun) table; unknown tools → "called N tools"; edits first, then reads, then commands; >4 segments → "+N more"; a tool-less reply under 2 s prints nothing. **Failed tool calls are not counted**, because "edited 2 files" when one write was denied is the over-claim to avoid.
**Chelys:** Telegram prefixes `[used list_dir]`. Checked: the `tool` event fires after approval and before the handler, so **denied** calls are already left out, but calls that **error** (`ToolError`/`OSError`) are still listed. That's a small over-claim; fix it by having the tag count only successful calls. A Chelys-sized version of this line (elapsed + verbs) would be ~30 lines, nice for the terminal and Telegram. Low priority, pure polish.

## turn_usage.py (304 lines) — counting tokens after every call

`record_response_usage`, after each successful call:
- **Count the call even when usage is missing**, and log `in=? out=?` instead of skipping.
- Normalize the provider's usage into canonical buckets: prompt, output, cache_read, cache_write, reasoning.
- Feed the compressor (its idea of how full the window is comes from *real* provider numbers, not estimates). Rearm the compression budget once a compaction is confirmed to have worked.
- Add to per-session counters; estimate cost (`usage_pricing`); queue (don't write; a cold SQLite UPDATE stalled the loop) per-call deltas to state.db.
- **One log line per call:** `API call #N: model=… in=… out=… total=… latency=…s cache=hit/prompt (pct%) write=… id=…`. The comment explains why every field is there: diagnosing cache misses once took a DB join and a live probe because `write=`, `id=` and the upstream weren't on the line.
- The MoA (mixture-of-agents) folding, Gemini multi-hop prompt correction and Nous wire switching don't apply to us.

**Chelys today:** `provider.py` returns `_usage`, and `agent.py` **pops it and throws it away**. DeepSeek reports `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`, and cache hits are ~10× cheaper, which is the whole point of keeping the system prompt byte-stable (conversation_loop shortlist item).
**Port:** keep usage; per-call log line (`in, out, cache_hit/miss, latency`); per-session totals in the sessions table (two integer columns). Small. It's also how we *verify* the byte-stable prompt works: the cache-hit % should climb across a conversation.

## turn_explainers.py (275 lines) — the failed-write footer and "why no reply" text

Backs the finalizer's items 7 and 8.
- `_record_file_mutation_result`: for `write_file`/`patch`, **keep the first error per path**. A later success on the **same resolved file** (absolute, `normcase`d, so `notes.md`, `/repo/notes.md` and `Notes.md` on Windows all match) clears it. On failure it also saves the file's `(mtime_ns, size)`.
- `_file_mutations_still_failed`: at turn end, drop entries whose file **changed on disk anyway** (e.g. the model gave up on `patch` and wrote it via the shell). The stat signature catches writes the recorder never saw. Clever and cheap.
- Footer: up to 10 paths with their first error, then "+N more". Paths are backticked so the gateway's auto-attach-local-files feature won't send them. That's a security detail: a footer must not leak a protected file as an attachment.
- `_format_turn_completion_explanation`: exit reason → a fixed, localized explanation (exact match, then prefix match like `max_iterations_reached(…)`). Healthy `text_response(…)` and unknown reasons produce nothing (don't second-guess). The empty-reply text: "{model} didn't produce a reply this time, even after retries. Send `continue` to try again…"
- Each feature has a config/env switch, default on, failing safe to on.

**Chelys:** if we port the footer, port the stat trick with it (we have `run_shell`, which can write files the recorder doesn't see). Path identity: `os.path.normcase(os.path.realpath(...))` is enough. ~30 lines total. A fixed exit-reason → text table is a good shape for our existing "[stopped…]"/"[this turn failed…]" notes, keeping them in one place.

## turn_liveness.py (250 lines) — the "this turn is stuck" watchdog

A background poller (every 15 s) watches the agent's activity clock, which every phase "touches." If nothing has touched it for **600 s**, it force-interrupts the turn and stops renewing the session lease, so the session can be reclaimed.
- **Race safety:** the abort decision is tied to the observed `(generation, timestamp)` and re-checked under the same lock that stamps activity. A turn that woke up in the meantime is never killed.
- **Time asleep doesn't count** (`AwakeIdleMeter`): a laptop lid closed for an hour isn't a stalled turn. *Very* relevant to a phone that dozes.
- Before aborting, dump **every thread's stack** to the error log, marking the turn thread. That's the only evidence of *where* it wedged.
- Two messages: "attempting recovery" (observational) and "aborted" (only after the abort actually committed). Never claim an outcome that hasn't happened.
- Config values are validated: NaN would silently disable the timeout, Inf would freeze the poll loop.

**Chelys today:** no watchdog. Each HTTP call has `request_timeout=60`, but in urllib that's a **per-socket-operation** timeout, not a total. A server dripping a byte every 50 s never times out. `run_shell` has a hard 30 s kill. In the Telegram bridge one stuck turn blocks everything (it's a single loop), including the owner.
**Port candidate (medium, later):** not the thread machinery. Simplest is a **total deadline per API call** (track `time.monotonic()` while reading the body in chunks and abort past N seconds), plus a turn-level wall-clock cap checked between steps. If we ever thread the bridge, copy the "time asleep doesn't count" rule; on Android that's measured by comparing `time.monotonic()` (which pauses in deep sleep on Linux) with `time.time()`. **Needs a check on the device**, because Android's clocks have quirks.

## turn_facade.py (224 lines) — the front door: `AIAgent.run_conversation`

A wrapper around `conversation_loop.run_conversation`. Before the turn: cancel any background review sharing this session; **admit a durable turn lease** (turn_facade_lease.py) so two processes can't run turns in the same session at once; set up relay/accounting/portal context vars (Nous-platform plumbing). After: record the outcome (`success`/`failed`/`cancelled`/`timed_out`), then in a cascade of nested `finally` blocks release everything in reverse order. Each release is in its own `finally`, so one failing release can't skip the rest. `chat()` is the one-liner: `run_conversation(msg)["final_response"]`.
**Takeaway for Chelys:** the nested-finally release pattern, plus the question of **two writers on one session.** Today `chelys chat --session N` on the Telegram session, while the bridge runs, would interleave two conversations' turns in one history. Atomic `append_turn` keeps rows intact but the *order* goes wrong. Rare for one owner. Cheap guard if wanted: a lock file per session (`os.open(..., O_CREAT|O_EXCL)`) or a `locked_by_pid` column. Note it, don't build it yet.

## turn_facade_lease.py (491 lines) — one writer per session, across processes

The answer to "two writers on one session" above, done properly:
- **Lease row in state.db**: holder = `pid=…:turn=…:platform=…`, **TTL 300 s**, renewed every 60 s by a timer. A crashed process just stops renewing, and after 5 minutes anyone can reclaim it. No stale-lock cleanup needed; the TTL *is* the cleanup.
- Admission **waits up to 30 min** for another holder, telling the user "⏳ Another Hermes process is using this session…". It can be interrupted, and gives up with a clear "your message was not processed, send it again."
- **Reload the transcript after admission** if it had to wait: the previous holder may have added turns or compacted the session meanwhile. If it got in immediately, don't reload (that would needlessly break prompt caching).
- Renewal can hit a SQLite lock. It retries only while the *next* attempt could still land before expiry; otherwise it **interrupts its own turn** "to protect the transcript," because past expiry a successor may already own the row. Persist writes are **fenced by holder**, so a late flush from a lost lease can't clobber the new owner.
- Lease renewal ≠ progress: that's why the liveness watchdog exists separately (a wedged turn would renew forever).
- If a follow-up message interrupts the wait, the earlier never-processed input is carried forward rather than dropped (unless the stop was a hard `/stop`).

**Chelys:** this is the industrial version of the lock-file idea. If we ever need it, the minimal Chelys version is: a `lease_holder, lease_expires` pair on the sessions row, set with one `UPDATE … WHERE lease_expires < now OR lease_holder = me`, checked with `rowcount`, TTL ~5 min, renewed between model steps (no timer thread; our steps are < 60 s), and **reload history after waiting**. ~25 lines. Only needed when two front-ends share a session, which we don't do yet. Keep it on the "later" list.

## turn_failure_copy.py (481 lines) — what to tell the user when a turn fails

All the failure text in one module. Every terminal result gets a `failure_reason` code plus `failure_retryable`, so UIs show something specific instead of "unknown."
- **Copy style rule:** say *what happened* and *what to do* in plain words; the raw provider text goes on a trailing `Provider said:` / `Details:` line. Example: "{label} rate-limited every one of {attempts} attempts — it looks temporarily unavailable. Wait a minute and send /retry…

Provider said: …". If the provider gave a reset time, name it ("resets at 14:05 (in 12m)") instead of "wait a minute."
- **Failed-turn boundary row:** a turn that ended without an assistant row gets one written by Hermes, **with no provider detail in it**. And `failed_turn_notice`: **"never claim 'not processed' when a tool may have run."** If any tool call is in the turn, the text becomes "Some actions may already have run; verify their effects before resending."
- `exit_reason_failure`: table of exit reason prefix → (code, retryable, fails_turn). "Advisory" verdicts add a code without flipping `failed`, so cron/kanban behavior keeps keying on the right thing.
- `truncated` vs `truncated_unreported`: if the model didn't report hitting the limit, **don't claim it did**. Say "most likely a dropped connection or a router cutting the reply short." Accuracy over a tidy story.
- `short_detail(exc)`: first line of the exception, capped at 200 chars.

**Chelys today:** already close in spirit. `_http_error_text` maps 401/402/429/5xx to plain hints plus the provider's message (capped at 300). `_save_aborted` names tools that already ran, which is the same principle as `failed_turn_notice`. **Small port:** reword the abort note so that when tools ran it says "some actions may already have run, check before resending," and put the provider detail after the plain sentence rather than in parentheses. Our messages are fine; this is polish.

## turn_preflight.py (396 lines) — "compress before sending?"

Two gates around context compression (the compressor itself is in context_compressor.py; not a turn file, and not read yet).
- **Pre-API gate** (`run_preflight_compression`), once per call: if compression is on, attempts remain, there's no failure cooldown, the estimate isn't "noisy," and `should_compress(pressure)` says yes, then summarize old history (an LLM call) and **refund** the iteration (it never reached the provider). A lock/cooldown skip is a *defer*, not proof the history can't be compressed, so it refunds the attempt too. If the provider already **proved** the request doesn't fit (413/overflow) and compression can't run → **fail closed** rather than resend (llama.cpp silently truncates oversized prompts).
- **Post-tool gate** (`compress_after_tool_results`): big tool results are what blow the window. Pressure = **the provider's last real `prompt_tokens` plus a rough estimate for only the rows added since** (the "usage anchor"). Use prompt tokens, never completion: thinking inflates completion with tokens that don't occupy context. Right after a compaction there's no real count yet, so don't act on rough guesses.
- If over the threshold but compression is blocked: warn (deduped), and run **`prune_tool_results_only`: deterministic, no LLM.** Old tool-result bodies are replaced by one-line markers, keeping the recent tail. Committed only when it reclaims enough, so prompt-cache breaks stay rare. After a prune, the "file unchanged since you last read it" dedup cache is reset, otherwise a re-read would be refused while the content is gone.
- Housekeeping note: lines 321–344 repeat the same comment four times. Leftover merge noise. Not everything in Hermes is polished.

**Chelys today:** no compression. `store.recent_window` sends the newest slice under `history_chars=40000` (≈10k tokens), starting at a user message, and older turns just fall off. That's crude, but it can't overflow DeepSeek's window, and it's free.
**What's worth taking now:** the **deterministic tool-result prune** idea, not LLM summarization. Tool outputs (file reads, shell output, up to 6000 chars each) are the bulk of history. Shrinking tool results *older than the last N turns* to `"[tool output from earlier turn, N chars, dropped]"` would let far more real conversation fit in 40k chars. **Do it in `recent_window` at send time, not in the DB**, so it's reversible and costs nothing. Caveat: changing older bytes breaks DeepSeek's prefix cache. Fine, *if* the cut point only moves occasionally (e.g. prune in blocks), same reasoning as Hermes's "episodic cache breaks." Medium-small. Real summarization stays "large, later."

## Four small ones

**turn_scripted_prelude.py (63)**: a caller can pass a generator that "plays" tool calls *before* the first model call (used for the desktop onboarding). Each step yields `(text, tool, args)`, runs through the normal `run_tool_round`, and gets the result sent back in. Call ids are deterministic (session + args + position) so replays are byte-stable. **Chelys:** not needed. Mildly interesting as a way to script a first-run "let me check your battery and storage" intro.

**turn_recovery_codex.py (76)**: Codex-only. When the backend rejects stale encrypted reasoning blobs (sometimes disguised as a 401 `token_expired`), strip them and retry once; if it happens again, turn replay off for the session. The bounded escalation (strip some → strip all → disable) is the familiar pattern. **Chelys:** skip.

**turn_author.py (103)**: who wrote the user side of a turn when bots/agents message each other (`HERMES_TURN_AUTHOR` env, `bot:<host>/<profile>` ids). Input hygiene worth copying: `_clean_text` **drops Unicode control/surrogate/unassigned/private-use categories (`Cc, Cs, Cn, Co`)** but keeps format characters, so emoji ZWJ sequences survive, and caps at 200 chars. The env var is **popped**, not just read, so child processes don't inherit it. **Chelys:** if the Telegram bridge ever puts a display name into the prompt ("message from Alex"), clean it this way first. Telegram names are attacker-controlled text.

**turn_recovery_autorecover.py (116)**: after retries **and** fallbacks are spent, if the error is transient (**5xx, overloaded, timeout only**) and **nothing has been shown to the user yet**, don't give up: park with a visible countdown, *"⏳ Provider temporarily unavailable — retrying automatically in 30s (cycle 2/5); send /stop to cancel"*, then rerun the normal retry loop. Schedule: jittered **15/30/60/60/60 s**; a provider `Retry-After` wins, capped at 120 s. Auth, format, policy, billing, and overflow errors never enter. Default 5 cycles; 0 disables.
**Chelys:** **this is the one for the Telegram bridge.** On a phone with flaky mobile data, a DeepSeek 503 or a timeout currently ends the turn with an error. With this ladder plus a short in-call retry (turn_api_error notes) the bridge rides out blips by itself. The countdown matters on Telegram: one "retrying in 30s" message beats silence. Port together with the basic retry; ~40 lines more. Needs: classify errors into transient vs not (we have the HTTP code and the exception type, enough), and the bridge must still answer `/stop` during the wait. Today the bridge is single-threaded, so the wait would have to poll Telegram in between. **Design question for the user.**

## turn_overflow.py (511 lines) — the provider said "too big"

Runs after the error is classified, **before** the generic 4xx handler (otherwise an overflow would be treated as "bad request, give up").
- **413 payload too large** is a *byte* problem. Compress, then measure progress in **serialized payload bytes**, never the token estimate. The estimator prices each image at a flat token cost, so freeing megabytes of base64 looked like "no progress," burned every attempt, and wedged the session for good. If compression can't shrink it, strip retained images from tool results; else fail with a clear message.
- **Context-length errors come in two shapes, and treating them the same is a bug:**
  - *"max_tokens too large"*: the input fits but input + max_tokens > window. Fix the **output cap**: `min(provider's available, window − our estimate) − 64`. Compress a little too, but never let a compression error make this fatal. If it's an output-cap error and the limit can't be parsed from the message: **fail fast**, since compression can't fix it and would loop on the same 400.
  - *"prompt too long"*: the input overflows. If the provider states its real limit, adopt and persist it (never shrink the window from *guesses*). Then compress, measuring progress in tokens *or* message count (pruning can shrink tokens without shrinking the list). Progress → retry; none → fail with "too long, can't shrink, /new."
- **Unexplained rejection:** on a *local* server, if the request is under half the known window and the server quoted no numbers, **don't compress** (that destroys history for nothing). Another request is probably holding the server's slot, so say "wait and /retry." On hosted APIs, believe the rejection and compress.
- Every attempt counts toward `max_compression_attempts`, so no path loops forever. A 2 s pause between compression retries.

**Chelys:** the sliding window (40k chars ≈ 10–13k tokens) + 6000-char tool clip keep us far below DeepSeek's window, so overflow is near-impossible today. The output-cap case *is* reachable if someone sets `max_tokens` above DeepSeek's per-model cap. **Port (tiny):** if DeepSeek's 400 mentions max_tokens, say "max_tokens in config.toml is above this model's limit" instead of a raw 400. Everything else here waits until we have compression.

## turn_context_compaction.py (512 lines) — compaction at the start of a turn

Two passes before the first model call, plus the shared helpers the other compaction sites use:
- **Idle compaction** (opt-in, `idle_compact_after_seconds`): if the session sat idle long enough and the history is above the post-compression target, compact *now*. My read of why (inference, not stated in the code): after a long idle the provider's prompt cache has expired anyway, so compacting then costs no extra cache miss, while compacting mid-conversation throws away a warm cache.
- **Preflight compression**: a cheap pre-check (`_should_run_preflight_estimate`) gates the full token estimate. **Rough estimates that aren't anchored on real usage are deferred** until a real provider count arrives. Up to `max_compression_attempts` (3) passes; each pass must make progress in **rows or tokens**, else fail closed and stop. If a pass gets <5% of the way while still over the window, stop: "no further pass will get under it."
- `_refund_api_call`: anything that never reached the provider gives the iteration back. That's why compaction doesn't eat into the 8-ish step budget.
- `_reset_retry_state_after_compaction`: a compacted request gets a **fresh** set of empty-response/prefill counters.
- `_reanchor`: after compaction rebuilds the list, find this turn's user message again by exact content. List indexes are invalid after any rebuild.
- `_rearm_uncompressed_overflow_warn`: cheap gate, `chars ≤ context_length` (≈ ¼ of the window at 4 chars/token) means it can't be over, so skip the real estimate.
- Same story as before: lines 45–52 duplicate a comment. Lock-skips and cooldowns count as *defers*, never as "incompressible."

**Chelys:** nothing to port until we compress. Two principles to keep for when we do: (1) **measure from real provider usage, not estimates** (we'd need to keep `_usage`; see turn_usage); (2) **every compaction pass must show progress or stop**. Also note the idle-compaction cache logic. On a phone chatting in bursts with long gaps, "compact when coming back from idle" is the natural trigger.

## turn_context.py (1359 lines) — the turn prologue and the wire copy

Two jobs: `build_turn_context` (once per turn, before the loop) and `build_api_messages` (once per API call).

**`build_turn_context`, in order:**
1. Guard stdio against broken pipes (daemon/headless). Parse the turn author **first**, so a cached agent never carries the last turn's author into this one.
2. Bind turn identity: `task_id`, `turn_id`. A tripwire warns if a new turn starts before the previous one persisted (two turns interleaving).
3. **Reset per-turn state from one table** (`_PER_TURN_RESET_STATE`): every retry counter and nudge flag the other phases use goes back to 0/False, so no counter leaks across turns. **Freeze the turn clock** (`_current_turn_timestamp`) "so every request this turn sends identical bytes." New `IterationBudget`. Clean up dead TCP connections from a previous provider failure.
4. Log one INFO line: `conversation turn: session, model, provider, platform, history=N, msg='first 80 chars'`.
5. Append the user message; hydrate per-session counters from history on the first resumed turn.
6. **System prompt: built once per session, cached, and restored from the DB on resume.** Never rebuilt per turn. The session row is created *after* the prompt exists, otherwise it stores NULL and the first resumed turn misses the cache.
7. Turn-start compaction (idle, then preflight).
8. Collect per-turn context: `pre_llm_call` plugin hooks (oversized output **spilled to disk** so a runaway plugin can't bloat every later prompt), gateway notes, memory prefetch (skipped for trivial prompts). **All of it goes into the user message, never the system prompt.**
9. Stamp the **`api_content` sidecar**: the exact bytes sent for this user message (text + injected context) are stored next to the clean text. Next turn, history replays *those bytes*, so the prefix the provider cached is reproduced exactly.
10. **Persist the user turn before the first model call** (crash resilience).

**`build_api_messages`, per call:** clone every row structurally (never mutate history through nested dicts); pop bookkeeping fields (strict APIs reject unknown keys); for historical rows, **replay the `api_content` sidecar**; for the current row, reuse the prologue's stamp; copy reasoning per route policy; drop `finish_reason`; prepend `system = cached prompt (+ ephemeral prompt)`.

**The design principle, from the docstring:** *"the system prompt is built ONCE per session and replayed verbatim (stable cache prefix)."* Everything that changes per turn rides in the user message.

**Chelys today (checked agent.py):** `system_prompt(s)` is rebuilt **on every model step** and includes `Local time: %Y-%m-%d %H:%M %Z`. Two consequences:
1. **The prefix cache breaks every minute.** DeepSeek caches by prefix. The time sits near the top of the system prompt, so everything after it (all history) is a cache miss whenever the minute changes. In a long chat, nearly every turn pays full price for the whole history.
2. The sliding `recent_window` also shifts its start as old turns fall off, which changes the prefix again. Smaller effect: it happens once per dropped turn, not every minute.
**Fix (small, high value):** (a) drop the time from the system prompt and prepend `[local time: …]` to the **current user message** at send time; (b) store the system prompt per session (one column) and reuse it, rebuilding only on `/new` or persona change; (c) make the window drop old turns **in blocks** (e.g. cut back to 60% when it hits 100%) so the prefix stays stable between cuts. Verify with DeepSeek's `prompt_cache_hit_tokens` (turn_usage port), which should go from ~0 to most of the prompt.
**Also:** Chelys saves the user message only at turn end. If the phone dies mid-turn, a Telegram message is gone from history. Persisting the user row at turn start is the same decision as "persist as you go" (turn_tool_round notes).

## turn_recovery.py (1978 lines) — what happens when the model call throws

About two thirds of this file is provider plumbing that will never touch Chelys: Nous free-tier route healing, Codex/xAI/Vertex/Copilot/Anthropic OAuth refresh, Bedrock SDK fallback, Anthropic fast-mode and 1M-beta, Z.AI overload schedules, llama.cpp grammar, credential-pool rotation, image shrink/strip. Read, noted, skipped. The parts that matter:

**The pipeline when a call raises** (the order is load-bearing; each step can `return` early):
1. `recover_before_classification`: repairs that need no classifier. UnicodeEncodeError: strip lone surrogates (clipboard paste) from history, else, only if the *process* locale is really ASCII, strip non-ASCII from the request copy. Never trust the error text alone ("provider-controlled and can mention ascii"). Max 2 passes. Also: a non-ASCII char in the **API key** (bad paste, `ʋ` for `v`) is stripped with a warning.
2. `classify_api_error` (error_classifier.py, 1577 lines, not read) → a `FailoverReason` plus `retryable`, `is_auth`, `error_context`.
3. `recover_after_classification`: one-shot repairs, each guarded by a `TurnRetryState` flag: 402 "can only afford N tokens" → retry once with `max_tokens=N`; reasoning-mandatory route → stop sending the disable; 401 → refresh credentials once; format errors → strip the offending field once.
4. `route_classified_error`: overflow with compaction disabled → terminal; rate-limit/billing → eager switch to a fallback model; transport failure → fallback after 2 retries; auth surviving refresh → fallback once.
5. Then overflow handling (turn_overflow), then either a terminal result or `compute_error_backoff` + `interruptible_backoff_sleep` and try again.

**Rules worth keeping, provider-independent:**
- **Repairs mutate the per-call copy, never the canonical history.** Comments cite real incidents where stripping images/non-ASCII from `messages` permanently deleted them from the DB. Strip `api_messages`, leave `messages` alone.
- **Retry only if something changed.** "If nothing was repaired, an identical retry cannot succeed — return False", otherwise the loop burns its passes resending the same bytes.
- **Don't grow a failing session.** On a 400 with a big request (>50k tokens or >80 messages), skip persisting the failed turn: "persisting the failed message grows the session and repeats the failure." Likely context overflow.
- **Keep delivered partial text.** If text was already shown and then retries die, the reply is that text plus the error, flagged partial, not an error alone.
- **Log every attempt with its attempt number and whether it's retryable** ("a bare 'attempt 1/3' promises a second attempt that never comes"). Retry chatter is buffered and shown only if everything fails; a long wait (>60 s) is announced live.
- **Terminal copy says what happened and the next step**, classified per reason (`exhausted_copy` / `nonretryable_copy` in turn_failure_copy). For rate limits it adds *when the limit resets* if the provider said so ("a per-minute throttle and a 13-minute plan window look identical without it").
- **Interruptible sleep:** wait in 0.2 s slices checking the interrupt flag; on interrupt, close any open tool sequence, persist, return `interrupted`.
- `_failure_hint_for` guesses from code + elapsed time: 524/504 timeout, 429, 5xx, else "fast response (<10 s) likely rate limited" / "slow (>60 s) likely upstream timeout".

**Backoff numbers** (`compute_error_backoff` + retry_utils.py, checked):
- Provider `Retry-After` header, or a `retry_after` field in the body, wins for **any** retryable error, not just 429 ("ignoring it turns an origin outage into a retry storm"). Capped at **600 s** (`RETRY_AFTER_CAP_S`; raised from 120 because Anthropic buckets reset in ~171 s). Zero or expired means none, so it can't hot-loop.
- Otherwise `jittered_backoff(attempt, base_delay=2.0, max_delay=60.0)`: `min(2 * 2^(n-1), 60)` plus 0–50% random jitter. So about 2–3, 4–6, 8–12 s… Default max retries is 3 (`api_max_retries`).
- Note the autorecover ladder (after all this fails) has its own 120 s cap; the two caps are different on purpose.

**Chelys today (checked provider.py, 131 lines):** **zero retries.** Any 429, 5xx, timeout, DNS blip or dropped connection ends the turn with a `ProviderError` and a saved note. On a phone on mobile data, a transient failure is the *most common* failure, so this is the biggest robustness gap in the brain. `_http_error_text` already gives good per-status copy; what's missing is the loop.

**Port (small, stdlib, high value):** wrap the `urlopen` in `provider.chat` with:
- retryable = 429, 500/502/503/504/529, timeout, `URLError`/`OSError` (connection reset, DNS). Not 400/401/402/403/404/422.
- 3 retries; wait = `Retry-After` (from `e.headers`, capped) else `min(2·2^(n-1), 60)` + jitter.
- sleep in short slices so Ctrl-C and Telegram `/stop` still work (the bridge is single-threaded, same design question as autorecover).
- log `attempt n/3 failed: <summary>, retrying in Xs`; the user sees it only if all retries fail, or live if the wait is long.
- **Never retry after tool results were delivered/streamed?** Not a concern: Chelys doesn't stream, and a failed *request* means no tool ran from it. A retry is safe because the request is idempotent from Chelys's side.
- Skip later: classifier, fallback chain, credential refresh. One provider, one key.
