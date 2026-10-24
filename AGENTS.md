# AGENTS.md: Chelys

Chelys is a stdlib-only agent for 32-bit Android (Termux), inspired by Hermes Agent. Setup and usage are in [README.md](README.md). Hermes attribution is in [NOTICE](NOTICE).

## Goal

Make Chelys behave as much like Hermes Agent as a 32-bit phone allows. Port actual Hermes code where a piece can be lifted without dragging in Hermes's dependency tree, and rebuild the behavior otherwise. Hermes's reduced `[termux]` install set is a useful map of which features are worth porting first. Copied Hermes code keeps its MIT notice, and NOTICE lists it.

## Rules

- Standard library only. No pip dependencies. Anything built with Rust or C will not install on the target phone (armv8l, `arm-unknown-linux-androideabi`).
- Never disable TLS verification. Never print, log or commit API keys or bot tokens.
- Keep the mandatory import path small. Optional features (Telegram, TTS, HTTP fetch) go in modules that nothing imports by default.
- Risky tools must set `risky=True` so they go through the approval gate.
- Don't install background services, and don't claim Android background reliability, without testing on the device.
- Mock tests are mocks. Claims that something works live need a run on the phone.
- The repo is meant to become public. Keep personal details (IPs, hostnames beyond the `galaxy-a10e` alias, account info) out of it.

## Porting from Hermes

Port one subsystem at a time (memory, then skills, and so on), not all of Hermes at once. Read before implementing: read the Hermes source for that subsystem yourself rather than going from memory or a summary, and write notes in `docs/hermes/` as you go. Notes record what Hermes does, its real numbers and rules, and what Chelys should keep or drop for a phone. If you skip a file, say so in the notes and why. Finish with a short prioritized list of what to port, agree on it, then build. Rebuild behavior in your own words; don't paste Hermes code. Notes about "Chelys today" are snapshots, so update the summary, not old notes, when things change.

## Map

| File | Role |
|---|---|
| `chelys/agent.py` | turn loop, tool dispatch, limits, truncation handling, grace call, abort notes |
| `chelys/guardrails.py` | tool-loop guard (warn/block/halt thresholds from Hermes) |
| `chelys/repetition.py` | detects repetition-dominated replies |
| `chelys/compaction.py` | summarizes older history (rebuilt from Hermes's context_compressor) |
| `chelys/provider.py` | DeepSeek HTTP client, retry/backoff, message shaping |
| `chelys/tools.py` | tool schemas and handlers |
| `chelys/store.py` | SQLite history saved as it happens, soft-archive, turn lease, history window, usage log |
| `chelys/config.py` | settings, `config.toml`, key loading |
| `chelys/cli.py` | `chat`, `ask`, `sessions`, `show`, `usage`, `telegram` |
| `chelys/telegram.py` | Telegram long-polling bridge, owner-only, approval buttons, outage countdown with `/stop` (imported only by `chelys telegram`) |
| `scripts/deploy.sh` | copy to the phone over SSH and run the tests there |
| `docs/hermes/` | reading notes on Hermes; port lists in [brain_summary.md](docs/hermes/brain_summary.md) (turn loop, done) and [skills.md](docs/hermes/skills.md) (skills, not started) |

Phone access from the dev PC: `ssh galaxy-a10e`.

## Status

v0.1.0 works on the device. Verified on the phone on 2026-10-24 (SM-S102DL, Android 11, Termux Python 3.14.8 armv8l, `deepseek-flash` with thinking off):

| Check | Result |
|---|---|
| Test suite | all 24 tests pass |
| Live tool use | the model chose `battery_status` on its own and answered from the result |
| One turn | 5.8 s, 18.5 MB peak RSS |
| Startup | 0.8 s, 16 MB |
| Resume | a fresh process resumed the conversation and recalled the earlier answer |
| Failure paths, live | missing key and bad config exit 2; rejected key (401) and timeout exit 1; risky tool refused in `ask` with nothing written; Ctrl-C stops in 0.17 s, exits 130 and saves a "cancelled" note |

Not established yet: running unattended or overnight, surviving a reboot, and how often Android kills Termux.

Telegram bridge: 39 tests pass on the PC and on the phone (fake Telegram API), including guests (`telegram_guests`: own conversation, risky tools always refused). It ran live on the phone on 2026-10-24 and the owner had a real chat through it. The approval buttons have not yet been pressed in a live test. Hermes's Telegram adapter (7,381 lines on python-telegram-bot, httpx and asyncio) was not portable, so its behavior was rebuilt with the standard library.

Ported from Hermes since v0.1.0: all 13 items in [brain_summary.md](docs/hermes/brain_summary.md). Items 1-8 (retries, cache-stable prompt, usage log, truncation, tool-call fixes, grace call, loop guard, `[used ...]` tag) were verified live on the phone on 2026-10-24: 62% cache hits on a short chat and a correct tool tag. Items 9-13 (persist as you go, request deadline, auto-recovery, compaction, turn lease) pass 101 tests on the PC but have not run live yet. Old databases are migrated in place on open.
