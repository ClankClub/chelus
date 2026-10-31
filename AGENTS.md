# Chelus: agent instructions

Chelus is a Python standard-library-only AI agent for 32-bit Android/Termux, inspired by Hermes Agent. Setup and usage: [README.md](README.md). Verified state and gaps: [docs/STATUS.md](docs/STATUS.md). Hermes attribution: [NOTICE](NOTICE).

## Constraints

- Support 32-bit Termux (armv8l). No pip dependencies or native Python extensions; keep optional integrations off the default import path.
- Never disable TLS verification or print, log or commit credentials. This repo is public: keep personal details, IPs, account information and hostnames other than `galaxy-a10e` out of it.
- Preserve safety boundaries: risky tools set `risky=True` and use the approval gate; file tools protect config/data/`~/.ssh`; guests stay within `workdir` and cannot reach owner-only features. Shell access is not a sandbox.
- Tests with mocks are not live tests. Verify on the actual phone before claiming device or background reliability. Add regressions for failure paths.
- For Hermes ports, read the upstream source, document choices in `docs/hermes/`, agree on a prioritized port list before building, and tackle one subsystem at a time. Keep `NOTICE` and any reused-code license notices accurate.

## Code map

- `agent.py`, `guardrails.py`, `repetition.py`: turns, limits, tool dispatch and failure closeout.
- `provider.py`, `config.py`: HTTPS API, retry/backoff, response validation and settings.
- `store.py`, `compaction.py`: SQLite history, session ownership and summaries.
- `tools.py`, `approval.py`: local tools, file confinement, bounded shell capture, approval cards and grants.
- `web.py`, `spill.py`: `web_fetch`/`web_search` with the public-address guard, and long tool output saved for `read_file` paging.
- `skills.py`, `skill_tools.py`, `skill_guard.py`, `learn.py`, `review.py`, `curator.py`: skills, scanning, background review and archival.
- `memory.py`, `session_search.py`: MEMORY/USER stores and FTS5 history search.
- `schedule.py`, `cron.py`, `cron_tool.py`, `cron_scan.py`: schedule parsing, scheduled jobs, the unattended runner and tick, the model's `cron` tool and the job-prompt scan.
- `cli.py`, `telegram.py`, `stats.py`: user interfaces and optional telemetry.
- All modules above are under `chelus/`. Tests live in `tests/`; `scripts/deploy.sh` stages and tests on the phone before installing.

Run `python -m unittest discover -s tests -t .` after code changes. Keep setup details in the README and evolving verification in `docs/STATUS.md`, not here.
