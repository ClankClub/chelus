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

## Map

| File | Role |
|---|---|
| `chelys/agent.py` | turn loop, tool dispatch, limits, abort notes |
| `chelys/provider.py` | DeepSeek HTTP client and message shaping |
| `chelys/tools.py` | tool schemas and handlers |
| `chelys/store.py` | SQLite history and the history window |
| `chelys/config.py` | settings, `config.toml`, key loading |
| `chelys/cli.py` | `chat`, `ask`, `sessions`, `show` |
| `scripts/deploy.sh` | copy to the phone over SSH and run the tests there |

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

Next: a Telegram bot interface that answers only the owner's user ID and asks for risky-tool approval with Yes/No buttons. Before writing it, check whether Hermes's Telegram gateway code can be ported.
