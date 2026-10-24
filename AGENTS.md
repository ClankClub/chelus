# Chelys

A Python standard-library-only agent for 32-bit Android/Termux, inspired by Hermes Agent. Setup, usage and design are in [README.md](README.md); current verification and device limits are in [docs/STATUS.md](docs/STATUS.md). Attribution is in [NOTICE](NOTICE).

## Instructions

- Standard library only: no pip dependencies or native builds. Target armv8l Android; Python 3.11+.
- Never disable TLS verification or print, log or commit API keys or bot tokens.
- Keep mandatory imports small. Optional features remain off the default import path.
- Risky tools set `risky=True` and pass through approval. Shell access is not a sandbox.
- Distinguish mock/local verification from live phone/provider checks. Never claim a phone check from mocks.
- Keep personal details, IPs, account information and hostnames other than `galaxy-a10e` out of this public repo.
- Add regressions for failure paths. Run `python -m unittest discover -s tests -t .` before committing.

## Hermes porting

Port one subsystem at a time. Read its actual Hermes source and record behavior, numbers, omissions and phone tradeoffs under `docs/hermes/`; finish the reading with a prioritized port list and agree on it before building. Rebuild behavior independently unless code is explicitly lifted with its MIT notice and a NOTICE entry. Update the current summary rather than rewriting historical reading snapshots.

## Code map

- `agent.py`, `guardrails.py`, `repetition.py`: turns, limits, tool dispatch and failure closeout.
- `provider.py`, `config.py`: HTTPS API, retry/backoff, response validation and settings.
- `store.py`, `compaction.py`: SQLite history, session ownership and summaries.
- `tools.py`, `approval.py`: local tools, file confinement, bounded shell capture and approval cards.
- `skills.py`, `skill_tools.py`, `skill_guard.py`: skill library, approved changes, scanning and undo.
- `learn.py`, `review.py`, `curator.py`: learning, skill/memory background review and archival.
- `memory.py`, `session_search.py`: MEMORY/USER stores, memory prompt snapshots and SQLite FTS5 session recall.
- `cli.py`, `telegram.py`, `stats.py`: user interfaces and optional telemetry.
- `tests/`: stdlib unittest suite; no real model/API credentials.
- `scripts/deploy.sh`: stage and test on `ssh galaxy-a10e` (or `CHELYS_HOST`) before copying into the install.
- `docs/hermes/brain_summary.md`, `skills.md`, `memory.md`: subsystem reading/port status. Memory and session search are built; the notes record their port choices.
