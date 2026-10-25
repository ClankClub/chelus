# Chelys — agent instructions

Chelys is a Python standard-library-only AI agent for 32-bit Android/Termux. Setup and usage: [README.md](README.md). Verified state and gaps: [docs/STATUS.md](docs/STATUS.md). Hermes attribution: [NOTICE](NOTICE).

## Constraints

- Support 32-bit Termux. No pip dependencies or native Python extensions; keep optional integrations off the default import path.
- Never disable TLS verification or expose credentials, account details, or private machine information in code, logs, or commits.
- Preserve safety boundaries: risky tools must have `risky=True` and use the approval gate; file tools must protect config/data/`~/.ssh`; guests stay within `workdir` and cannot access owner-only features.
- Tests with mocks are not live tests. Verify on the actual phone before claiming device or background reliability.
- For Hermes ports, read the upstream source, document choices in `docs/hermes/`, and tackle one subsystem at a time. Keep `NOTICE` and any reused-code license notices accurate.

## Code map

- `agent.py`, `provider.py`, `store.py`, `compaction.py`: agent loop, API, history.
- `tools.py`, `approval.py`, `guardrails.py`: tool execution and safety.
- `telegram.py`, `stats.py`: Telegram bridge and runtime telemetry.
- `skills.py`, `skill_tools.py`, `learn.py`, `review.py`, `curator.py`: skills and background review.
- `memory.py`, `session_search.py`: persistent memory and history search.
- `config.py`, `cli.py`: settings and command-line interface.
- All modules above are under `chelys/`. Tests live in `tests/`; deployment and optional helpers in `scripts/`.

Run `python -m unittest discover -s tests -t .` after code changes. Keep setup details in the README and evolving verification in `docs/STATUS.md`, not here.
