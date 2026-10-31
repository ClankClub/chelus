# Project status

Updated **2026-10-31**. This is a current-state summary, not a release announcement. Chelus is running on the owner's Samsung Galaxy A10e (Android 11, **32-bit Termux**) and is **pre-1.0**. Its core runs on Python's standard library without pip dependencies; model inference is remote.

**Important source/deployment distinction:** The live phone installation is ahead of the source currently checked into GitHub. Capabilities below marked *owner-confirmed live* describe that configured machine; they should not be read as a promise that a fresh clone includes every integration. Setup remains in [README.md](../README.md), and this page remains the status source of truth.

## Confirmed on the target phone (base runtime)

- DeepSeek terminal chat, model-selected battery tool, conversation resume, and basic error/cancel behavior.
- Telegram owner conversation through the on-phone bridge.
- Persistent owner memory carried into a new conversation; prior conversation content retrieved through session search with DeepSeek.
- Agent runtime, conversation storage, memory, skills, and Telegram bridge run locally on the phone. DeepSeek provides off-device model inference.

## Additional live capabilities (owner-confirmed 2026-10-26)

These are field results on the owner's installed system, **not yet a clean-install integration test of the public repository**.

- **AgentMail:** Chelus uses an HTTPS API to send/read/reply to email without running a conventional mail client. An outgoing email was received in an external mailbox on 2026-10-26.
- **Vercel deployment from Termux:** The Vercel CLI runs on the 32-bit phone with Node.js separately installed. Chelus has deployed preview and production applications. Remote Vercel builders perform the build work; there is no claim that modern Node application builds happen on the phone.
- **Durable job queue:** A deployed app uses Upstash Redis to preserve jobs independently of the handset's availability. The owner-restricted deployment is at https://chelus-brainstem.vercel.app/.
- **Wireless ADB:** Used for phone control, package inventory, debloating, and restoring packages in the owner's environment.
- **Web and storage:** Web research/scraping and MEGA cloud storage are in use; optional third-party software and remote services are outside the dependency-free Python core.
- **Procedural skills:** Local skill files can be loaded before a class of task and revised when a procedure proves wrong. Automated test coverage for skills is listed separately below.

## Implemented and covered by automated tests

- Telegram guest isolation and risky-tool refusal; skills, approval handling, review, and curation. Tests use mocked external services, including when run on the phone.
- History persistence, compaction, request recovery, and turn leasing. Not every path has an independent live-model test.
- Runtime telemetry footer and optional read-only ADB cache reader. Android sensor access varies by device; the sampler is started manually.

Mocked tests establish behavior of the checked-in code, **not** production reliability of each external integration.

## Outstanding for 1.0 and verification limits

- **Scheduling/cron:** the scheduler core is built: schedules, the job and run tables with at-most-once claims and catch-up, the tick in the Telegram bridge, and unattended runs that use only "Always" grants. A one-shot test job ran on time on the phone on 2026-10-28. The model's `cron` tool is built (owner only, asks before every change, strict prompt scan) and was checked live on 2026-10-28: the real model created a weekday job with a correct `[SILENT]` prompt, listed it and deleted it. The queued `run` action is covered by tests only. Delivery and failure handling were finished on 2026-10-31. A live run was delivered with a confirmed Telegram message id and the stop footer, and its next-turn note was stored. The retries for an unreachable model (5/15/30 min) and the three-failure nudge are covered by tests only. Script jobs and the Telegram `/cron` command followed the same day. A no-model battery script job ran live and its output was delivered. `/cron` and the script-plus-model path are covered by tests only. The port list is complete; the later extras are listed there. Port list and status: [docs/hermes/cron.md](hermes/cron.md). Jobs run only while the bridge runs; recurring jobs over days, Doze catch-up and restart after a reboot are not verified.
- **Tools:** Hermes's tools layer was read on 2026-10-31 against the phone's own tool history, where most shell calls fetch web pages or search text. The port list is in [docs/hermes/tools.md](hermes/tools.md). Items 1, 2 and 3 were built the same day: `web_fetch` and `web_search` (owner only, no approval, public addresses only), and long tool output saved for `read_file` to page.
  - **Live on the phone:** a real model turn chose `web_fetch` without being told to and answered from python.org with no approval card. Fetching, the head-and-tail cut, the saved copy (0700 folder) and `read_file` paging it, and refusals of a LAN address and `localhost` all ran on the phone.
  - **Not verified live:** `web_search` returned DuckDuckGo's rate-limit page during testing, on the PC and the phone alike, which share one public IP. The refusal message is correct, but no live search results have been seen yet. Brave Search is covered by tests only.
  - **Item 2, built the same day:** `run_shell` now waits up to 120 seconds (was 30), and slower commands run in the background for up to 30 minutes, with a Telegram message when they end and `shell_jobs` to list, read or stop them. On the phone, the suite's real-process tests pass (exit codes, stopping a whole process group, the time limit). Live: asked to run a 150-second command, the real model chose a background run on its own; the worker outlived the `chelus ask` that started it, and the bridge sent the finish notice 13 seconds after the end with a confirmed message id, and stored it as a note. Not verified live: the 30-minute limit, a stop through `shell_jobs`, and a run surviving a bridge restart (all covered by tests on the phone).
  - **Not built:** file search and find-and-replace edits (items 4 and 5), and voice notes (item 6, the owner's call).
- **Unattended resilience:** Android process survival, extended background operation, reliable missed-wake recovery, and automatic restart after a reboot still need explicit end-to-end verification.
- **Specific Telegram flows:** end-to-end approval-button interactions, newer skills flows, and memory-review/pending cards remain not independently verified in the live bridge.
- **Release parity:** the on-phone code (tool approvals, Telegram photos, skill size limits) is back in this repository as of 2026-10-28. Integration setup (AgentMail, Vercel, MEGA, ADB) still lives only on the phone and is not documented here.
- **Portability:** the Galaxy A10e/32-bit Termux is the demonstrated platform. Additional hardware/OS support should be claimed only after testing.

Run the source test suite with `python -m unittest discover -s tests -t .`. Report separately what passed in desktop mocks, tests executed on the phone, and actual live network/device use. Avoid converting either self-reported field results or mocked tests into stronger guarantees than they support.

## Audit fixes (merged 2026-10-28)

Seven 2026-10-24 audit findings are fixed: Telegram batch tails, session ownership during slow retries, executable launcher and canonical install URL, bounded shell output, staged deployment with true test status, provider response validation, and guest listing metadata filtering. Covered by mocked tests; not yet run on the phone.

## Consolidation and rename (2026-10-28)

`main` now joins three lines that had diverged: the GitHub docs, the audit fixes, and the code the phone was running, which had never been committed (Hermes's tool-approvals work plus edits made on the phone). The package, scripts and folders are renamed Chelys to Chelus; existing installs migrate their config and data folders on first start (see the README). Tests pass on the phone in a scratch copy; the migration has not yet run on the live install.
