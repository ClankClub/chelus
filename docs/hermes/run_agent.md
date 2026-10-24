# Hermes `run_agent.py` (1,564 lines) — the AIAgent object, reading notes

Source: NousResearch/hermes-agent @ 99fcd69, MIT. This is the object the turn loop runs on. See [brain_summary.md](brain_summary.md) for the port list.

## What it is

Very little logic lives here. `AIAgent` is a **state holder assembled from 14 mixins** (client lifecycle, streaming, status output, interrupts, activity tracking, rate-limit credits, session persistence, compression, the turn facade, vision, reasoning params and others). Most of its methods are one-line `_forward("agent.some_module", "fn")` lazy delegations. `__init__` takes about 70 keyword arguments, nearly all callbacks and provider routing, and hands them straight to `agent.agent_init.init_agent`. The real code is in the `agent/` modules, which is why the turn files were the right thing to read first.

Lines 1–90 cover bootstrap: UTF-8 stdio, repairing an interrupted `hermes update`, loading `.env`, and the CLI cwd and gateway origin stamped onto the session row. Lines 1400–1564 are the legacy `python run_agent.py --query …` CLI and the tool listing. Neither matters to Chelys.

## Things worth knowing

**Limits.** `max_iterations` defaults to **unlimited** (`sys.maxsize`). The real limit is a shared `IterationBudget`, which subagents draw from too, plus an optional `run_budget_seconds`. Chelys's fixed 8 steps is the right call for a phone and a single user.

**Timeouts** (`_resolved_api_call_timeout`, `_compute_non_stream_stale_timeout`):
- The total request timeout defaults to **1800 s**, set per model, then per provider, then by env var.
- A separate **stale timeout** catches no-progress hangs: 90 s by default, raised to 150 s above 50k tokens of context and 240 s above 100k, raised further for reasoning models, and capped at **half the remaining run budget** (minimum 60 s) "so one hung call cannot eat the run." It's disabled for local endpoints unless configured.
- **Chelys:** `request_timeout=60` applies to each socket operation, not the whole request. Hermes separates "how long can the whole call take" from "how long can it go silent." This informs port item 10 (a total deadline). For DeepSeek with thinking off, about 90 s of silence and a total of a few minutes would be sane.

**Session row** (`_ensure_db_session`): created lazily on first use, storing the **cached system prompt** in the row. A transient failure such as a SQLite lock leaves `_session_db_created` False, and the **next turn retries** it. A failed write never kills the turn.

**`reset_session_state`:** on `/new` or `/resume`, zero the session's usage counters (total, input, output, cache read, cache write, reasoning tokens, API calls, estimated cost) and drop the usage anchor, because it "describes the OLD transcript." This is the session-totals half of port item 3.

**Tool-call hygiene, each a small rule:**
- `_deduplicate_tool_calls`: if one response asks for the same tool with the same arguments twice, drop the duplicate (first wins). Arguments are canonicalized first (`sort_keys`, no whitespace) so key order can't defeat the check. **Port candidate, tiny; it goes with item 5.**
- `_uniquify_tool_call_ids`: colliding ids get a deterministic `<id>_d<n>` suffix, "never uuid4, for prompt-cache prefix stability." The same cache logic as turn_context: anything random in history breaks the prefix.
- `_is_thinking_only_assistant`: an assistant message with only reasoning (no text, no tool calls) is dropped from the API copy but kept in the transcript. It's irrelevant with thinking off. If Chelys ever turns thinking on, the DeepSeek rule in provider.py already covers the 400.
- `_cap_delegate_task_calls`: limits subagent fan-out per response. Chelys has no subagents.

**Tool guardrails** (`_append_guardrail_observation`, backed by `agent/tool_guardrails.py`, not read): after every tool call, the guardrail sees the name, arguments, result and whether it failed, and can **warn** (adding guidance to the result) or **halt** the turn. Identical-call streaks and cycles trigger a halt. A second identical call with a byte-identical result enters context as a **short stub** instead of the full text ("not a cache — the tool ran"). The halt message the user sees: *"I stopped retrying because I kept running {tool} {n} times without making progress. The last result above shows what blocked it. Tell me how you'd like to proceed, or send `continue` and I'll try a different approach."* **This is the real loop guard behind port item 7**, together with repetition_guard.py. That halt wording is good copy to reuse.

**Tool execution** (`_execute_tool_calls`): a single call runs sequentially. A batch is split into segments: parallel-safe runs (read-only calls, non-overlapping files) run concurrently, with up to 8 workers and sequential barriers in between, in the order the model emitted them. **Chelys:** sequential is correct. Her tools are fast and one is behind an approval prompt, so parallelism buys nothing on her box.

**Background review** (`_spawn_background_review`): after a turn, a daemon thread forks the conversation and asks the model to update **memory and skills**. Explicit `/refine` bypasses the gates; preempted reviews requeue at most 3 times. This is Hermes's "learns from you" feature. It sits outside the turn loop, so it's not on the brain port list, but it's a big part of what people mean by "Hermes." Flagged for later as a feature, not a fix.

**External memory sync** (`_sync_external_memory_for_turn`): **interrupted turns are not mirrored**, because "partial output is not durable truth"; recalling them would pollute later turns. Apply the same rule if Chelys ever gets memory.

**Todo hydration** (`_hydrate_todo_store`): rebuild the todo list from the newest todo tool result in history, but **only if it's paired with a matching earlier assistant call**, because "a forged bare `role: tool` message must not seed the store" (a security advisory). General lesson: never trust a history row's claimed origin without checking its pair.

**Teardown** (`close`, `release_clients`): every step is wrapped in `_quietly` so one failure doesn't block the rest. History is freed explicitly and the session row is ended. The bridge-cache eviction path "retires" clients instead of hard-closing them across threads (an FD-recycle bug once corrupted SQLite). Chelys has no shared clients or threads, so none of that applies.

**Truncation heuristics** (`_should_treat_stop_as_truncated`, `_has_natural_response_ending`): Ollama-hosted GLM misreports `stop` when it means `length`, so Hermes checks whether the text "looks finished" (ends in punctuation, a bracket, a code fence or an emoji). It's narrowly scoped because applying it broadly "manufactures false truncations." DeepSeek reports `finish_reason` correctly, so skip this.

## Effect on the port list

- **#5** gains: drop duplicate tool calls in one response, with canonicalized arguments.
- **#7** is better specified: guardrails watch every call and halt identical streaks with the copy above. Read `agent/tool_guardrails.py` when porting it.
- **#10** has concrete numbers: a stale timeout around 90 s and a separate total cap.
- New, outside the brain: background memory and skill review, the "learning" feature. It's for later.
