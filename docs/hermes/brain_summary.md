# Hermes's brain vs Chelys's brain

The conclusion of a line-by-line read of Hermes Agent's turn loop (NousResearch/hermes-agent @ 99fcd69, MIT): `agent/conversation_loop.py` plus all 33 `agent/turn_*.py` files, about 15,000 lines. Detailed notes are in [conversation_loop.md](conversation_loop.md), [turn_phases.md](turn_phases.md) and [run_agent.md](run_agent.md). This file is the current port list; it supersedes the shortlist in those notes.

## What the brain is

Hermes and Chelys run the same loop: send history to the model, run the tools it asks for, send the results back, and stop when it answers in plain text or hits a cap. Chelys does that in about 300 lines (`agent.py`, `provider.py`, `store.recent_window`). Hermes does it in about 15,000. The core loop is the same in both; the extra code is everything Hermes does when something goes wrong, plus support for a dozen providers.

Hermes's conductor (`_run_conversation_turn`) calls phases in order: prologue (`turn_context`), iteration prep, request assembly, preflight size gate, the API call with its retry loop, response checks, then either a tool round or a final answer, then the finalizer. Phases share one state object and return a verdict: fall through, continue, break or return.

The recurring design rules, which matter more than any single file:

- **Every recovery is bounded and one-shot.** Every nudge, retry and repair has a cap, usually 1 to 4, with a flag on `TurnRetryState`. Nothing can loop forever.
- **Repairs touch the per-request copy, never the saved history.** Several comments cite real incidents in which a "fix" deleted images or text from the database for good.
- **Synthetic messages are flagged and removed.** Continuation nudges and empty-reply prompts are marked and stripped or collapsed before saving, so they never pile up in history.
- **Retry only when something changed.** An identical resend after a deterministic error is wasted.
- **The system prompt is byte-stable for the whole session.** It is built once and replayed exactly. Anything that changes per turn (time, memory, notes) rides in the user message, and the exact bytes sent are stored so later turns replay them identically. That stability is what makes the provider's prefix cache hit.
- **The turn's clock is frozen,** so every request within a turn sends identical bytes.
- **Every tool call gets a result row.** Orphaned calls get a synthetic one; history never ends on a dangling call.
- **Tell the truth in failure messages.** Don't claim "not processed" if a tool may have run, and don't claim truncation the provider didn't report.
- **Measure, don't estimate.** Context pressure comes from the provider's real `prompt_tokens`. Usage is logged for every call.

## What Chelys has today

All 13 items are built. Items 1-8 are verified live on the phone (cache hits, tool tag); items 9-13 are unit-tested only. Rows are saved as they happen, and a crashed turn is closed honestly on the next run. Older history is summarized, not dropped. Outages on Telegram are waited out with a countdown. One turn runs at a time per conversation. The system prompt is built once with no clock, and each user message carries a `[sent ...]` stamp that is stored and replayed byte-for-byte.

The parts Chelys already gets right match Hermes's lessons: the failed-turn note (Hermes's `_close_durable_failed_turn`), tool errors returned as results, bounded tool output, the iteration cap, and the DeepSeek message-shaping rules.

## Port list, in priority order

Each item gives the Hermes source, the size of the change in Chelys and why it matters. 

| # | Status | What | Hermes source | Size | Why |
|---|---|---|---|---|---|
| 1 | done | **Retry model calls.** Retry 429, 5xx, timeouts and connection errors 3 times. Honor `Retry-After` (capped), otherwise wait 2·2ⁿ⁻¹ s plus jitter. Sleep in short slices so Ctrl-C still works. Never retry 400/401/402/403/404. | turn_recovery `compute_error_backoff`, retry_utils | ~40 lines in provider.py | Today one network blip kills the turn. On a phone on mobile data, transient errors are the most common failure. |
| 2 | done | **Cache-stable prompt.** Move `Local time` out of the system prompt and into the current user message, frozen per turn. Build the system prompt once per session. Trim the history window in blocks rather than one turn at a time. | turn_context, turn_request_assembly | ~30 lines across agent.py and store.py | The time is near the top of the prompt and changes every minute, so DeepSeek's prefix cache misses on the whole history almost every turn. The sliding window breaks the prefix again each time an old turn drops off. |
| 3 | done | **Log usage.** One line per call (prompt, completion, cache hit and cache miss tokens) plus per-session totals. | turn_usage | ~20 lines | Verifies #2 and shows what each turn costs. Chelys currently throws `_usage` away. |
| 4 | done | **Truncation handling.** Never run tool calls from a `finish_reason == "length"` response. Retry once with a bigger `max_tokens`, then tell the model to write less. For plain text cut off by the limit, ask it to continue (max 4 times) and save one joined reply. | turn_truncation | ~60 lines | `max_tokens=2048` cuts off long `write_file` calls and long answers. Today a cut-off call becomes a misleading "bad arguments" error, and long answers end mid-sentence. |
| 5 | done | **Small tool-call fixes.** The unknown-tool error lists the valid names. Treat `""` arguments as `{}`. Detect cut-off arguments (no closing `}`) and say so. Drop duplicate calls (same tool, same canonical arguments) within one response. If the model returns an empty reply right after tool results, nudge it once. | turn_tool_validation, turn_empty_response | ~25 lines | Each one turns a dead end into a self-correction. |
| 6 | done | **Grace call at the iteration cap.** Instead of `[stopped: …]`, make one last call asking for a summary of what was done. Hermes keeps the tools in that request so the prefix cache still hits; Chelys does the same. | turn_iteration_prep, turn_finalizer | ~15 lines | The user gets an answer instead of a dead stop. |
| 7 | done | **Repetition guard.** Warn, then block or halt on repeated failures, repeated no-progress results and identical-call streaks (Hermes thresholds; `hard_stop` on for Telegram only). Separately, drop replies dominated by repetition. | tool_guardrails.py, repetition_guard.py (rebuilt, not copied) | `guardrails.py`, `repetition.py` | Cheap protection against loops that burn the iteration budget. |
| 8 | done | **Telegram `[used …]` tag.** Count only calls that actually succeeded. | turn_summary | ~5 lines | The tag currently lists calls that errored. |
| 9 | done | **Persist as you go.** Save the user message at turn start and the assistant tool-call row before running tools. Close orphaned calls with synthetic results on failure, and close a turn a crash left open at the start of the next one. | turn_context, turn_tool_round, turn_loop_errors | store.append, agent._close_turn, agent._close_open_turn | If her box dies mid-turn, the message survives. Replaced the atomic-turn design (approved 2026-10-24). |
| 10 | done | **Total deadline on API calls.** urllib's timeout covers each socket operation, not the whole request, so a slow trickle can hang the single-threaded Telegram bridge. Now `request_deadline` (300 s), checked between chunks. Certificate-verification failures are no longer retried (from error_classifier). | turn_liveness | small | Bridge robustness. |
| 11 | done | **Telegram auto-recovery.** After retries fail on a transient error (5xx, timeout, network; not 429), wait with a countdown and retry the same call: 5 cycles, 15/30/60/60/60 s plus 0-20% jitter, Retry-After up to 120 s. `/stop` cancels, and the bridge keeps polling during the wait. Interactive `chelys chat` does the same, with Ctrl-C to stop. | turn_recovery_autorecover | medium | The call is retried, not the turn, so tools never run twice. |
| 12 | done | **Compaction.** Summarize old history instead of dropping it, at turn start when history would overflow `history_chars`. Hermes's phases and numbers, applied to the character budget; see [compressor_and_errors.md](compressor_and_errors.md). | turn_context_compaction, context_compressor.py | compaction.py | Long-term memory within a conversation. |
| 13 | done | **Turn lease.** Stop two writers (terminal and Telegram) from interleaving turns in one session. A lease on the session row, TTL 600 s (longer than Hermes's 300 because there is no renewal timer); a dead holder's PID is reclaimed at once. | turn_facade_lease | store.acquire_lease | Each turn reloads history after taking the lease, so it sees the other process's turns. |

Not porting: the provider zoo (Nous, Codex, Anthropic, Bedrock, Vertex, Copilot, Z.AI), credential pools, fallback chains, OAuth refresh, image handling, streaming, plugin hooks and the desktop UI surfaces. Chelys has one provider and one key.

## Reading status

Read: the turn loop and all 33 `turn_*.py` files, `run_agent.py` ([run_agent.md](run_agent.md)), and `context_compressor.py` and `error_classifier.py` ([compressor_and_errors.md](compressor_and_errors.md)). Not read: `chat_completion_helpers.py` (4,014 lines of request building for the provider zoo; nothing ported needs it).
