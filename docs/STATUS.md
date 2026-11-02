# Project status

Updated **2026-11-01**. This is a current-state summary, not a release announcement. Chelus 0.9.0 is the 1.0 release candidate, running on the owner's Samsung Galaxy A10e (Android 11, **32-bit Termux**). Its core runs on Python's standard library without pip dependencies; model inference is remote.

The phone runs this repository's code, deployed with `scripts/deploy.sh`. Integrations set up by hand on that phone (email, Vercel, MEGA, ADB) are not part of the repository or of 1.0. Setup is in [README.md](../README.md); this page is the status source of truth.

## 1.0 checklist

1.0 means someone with an Android phone and Termux can install Chelus from the README, talk to it in the terminal and on Telegram, and leave it running: it comes back after a reboot and runs scheduled jobs.

- [x] Scheduling: jobs, delivery to Telegram, script jobs, `/cron` (2026-10-31).
- [x] Restart after a reboot through Termux:Boot (2026-11-01, about two minutes on the A10e).
- [x] Docs and product site match the code (2026-11-01).
- [x] A clean install from the public repository, in a fresh folder with fresh config, following only the README (2026-11-01 on the A10e: `ask`, memory, a note, `notes --check`, a scheduled job and chat worked; 475 tests pass on the clone).
- [ ] Telegram flows tapped through live by the owner: an approval card's buttons, a `/pending` card (skill, memory or note change), the `edit_file` diff card, the command menu.
- [ ] A week of unattended running with a daily job, its runs on record. The owner reports the bridge staying up for days, but no run since the reboot fix has been logged end to end.
- [ ] Tag `v1.0.0` and set `__version__`.

Not in 1.0: voice notes, notes in shared storage (built, unverified), and any other phone or OS.

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

## Verification details by area

- **Scheduling/cron:** the scheduler core is built: schedules, the job and run tables with at-most-once claims and catch-up, the tick in the Telegram bridge, and unattended runs that use only "Always" grants. A one-shot test job ran on time on the phone on 2026-10-28. The model's `cron` tool is built (owner only, asks before every change, strict prompt scan) and was checked live on 2026-10-28: the real model created a weekday job with a correct `[SILENT]` prompt, listed it and deleted it. The queued `run` action is covered by tests only. Delivery and failure handling were finished on 2026-10-31. A live run was delivered with a confirmed Telegram message id and the stop footer, and its next-turn note was stored. The retries for an unreachable model (5/15/30 min) and the three-failure nudge are covered by tests only. Script jobs and the Telegram `/cron` command followed the same day. A no-model battery script job ran live and its output was delivered. `/cron` and the script-plus-model path are covered by tests only. The port list is complete; the later extras are listed there. Port list and status: [docs/hermes/cron.md](hermes/cron.md). Jobs run only while the bridge runs; recurring jobs over days, Doze catch-up and restart after a reboot are not verified.
- **Tools:** Hermes's tools layer was read on 2026-10-31 against the phone's own tool history, where most shell calls fetch web pages or search text. The port list is in [docs/hermes/tools.md](hermes/tools.md). Items 1 to 5 were built the same day: `web_fetch` and `web_search` (owner only, no approval, public addresses only), and long tool output saved for `read_file` to page.
  - **Live on the phone:** a real model turn chose `web_fetch` without being told to and answered from python.org with no approval card. Fetching, the head-and-tail cut, the saved copy (0700 folder) and `read_file` paging it, and refusals of a LAN address and `localhost` all ran on the phone.
  - **Not verified live:** `web_search` returned DuckDuckGo's rate-limit page during testing, on the PC and the phone alike, which share one public IP. The refusal message is correct, but no live search results have been seen yet. Brave Search is covered by tests only.
  - **Item 2, built the same day:** `run_shell` now waits up to 120 seconds (was 30), and slower commands run in the background for up to 30 minutes, with a Telegram message when they end and `shell_jobs` to list, read or stop them. On the phone, the suite's real-process tests pass (exit codes, stopping a whole process group, the time limit). Live: asked to run a 150-second command, the real model chose a background run on its own; the worker outlived the `chelus ask` that started it, and the bridge sent the finish notice 13 seconds after the end with a confirmed message id, and stored it as a note. Not verified live: the 30-minute limit, a stop through `shell_jobs`, and a run surviving a bridge restart (all covered by tests on the phone).
  - **Items 4 and 5, built the same day:** `search_files` (regex or file-name search, ripgrep when installed, no approval) and `edit_file` (find and replace, with the diff on the approval card). On the phone, 405 tests pass, including both ripgrep and the Python fallback. Live: asked to change a line in a file and find which source files mention a name, the real model used `search_files` and `edit_file` on its own, and both results were correct. The Telegram card with the diff has not been seen live.
  - **Not built:** voice notes (item 6, the owner's call).
- **Skills from Hermes's bundled set (2026-11-01):** seven of Hermes's ~45 bundled skills were read; four were rewritten for the phone and ship in `skills/` (blocked-page recovery, grounded citations, arXiv, systematic debugging), and the phone's own web-search skill was rewritten around `web_search`/`web_fetch`. Live: asked for arXiv papers, the real model loaded the `arxiv` skill on its own and answered from the API with versioned IDs; given a Reuters page that returns 401, it loaded `blocked-page-recovery`, worked the ladder, found only a February snapshot and said so instead of passing it off as current. The same session found and fixed a bug: `skill_view` scanned the skill folder line it appends, and on the phone that path set off the secret guard, so every skill load carried a false warning. The Telegram bridge now registers its commands, so `/` and the Menu button list them (owner sees all, guests the short list); the registration was read back from Telegram, but the menu hasn't been looked at in the app yet.
- **Test bench (2026-11-01):** `bench/` runs Chelus in Docker on a PC against any OpenAI-compatible API (`provider = "openai"`, new) with a scored simulator (scripted or model-played owner, rule checks plus a model judge). First comparison, eight scenarios: DeepSeek Flash (via OpenRouter, not the phone's direct thinking-on setup) passed 29/31 checks, its two failures likely the judge's view of tool output being too short (since raised); GPT-OSS 20B passed 24/31, never loaded a skill, gave up early on a blocked page, and cited an arXiv ID and a URL no tool returned. A rerun with corrected cost accounting: DeepSeek Flash 31/31 for about $0.038, GPT-OSS 20B 26/31 for $0.0015 (25 times cheaper). DeepSeek runs now go to DeepSeek directly with the phone's settings; OpenRouter is kept for cheap non-DeepSeek models. The first run found an `edit_file` bug (a stray leading space in old_string was written into the file), fixed with tests. Bench results say nothing about the phone itself.
- **Notes (phases 1 to 3 built 2026-11-01):** a vault of linked Markdown notes, in the common format Obsidian and other editors read, beside the small memory stores. Six owner-only tools: `notes_search`, `note_read`, `note_write` (create, append, replace), `note_edit`, `note_move` (rewrites links across the vault) and `note_trash` (to `.trash/`, nothing erased); `/notes`, `/note NAME`, `/notes check` and `chelus notes` for the owner. The memory review also files details in notes: creates and appends apply, edits and rewrites wait on a card pinned to the note's revision. The weekly curator reports broken links, notes nothing links to and repeated names when they change. Built from public documentation only. On the bench (`notes-recall`, `notes-vault`, `notes-move`, `notes-review`) DeepSeek Flash passes; GPT-OSS 20B mostly does, its misses being skipped tools, claimed edits and, as a reviewer, about one run in five doing nothing. On the phone a 2,000-note vault indexes in 19 s and searches in about 210 ms, and a live rename and trash worked. Not verified: `notes_dir` on shared storage, because Termux's read permission for shared storage was revoked on the A10e (details in [docs/NOTES.md](NOTES.md)).
- **Unattended resilience:** restart after a reboot works: on 2026-11-01 an `adb reboot` of the A10e brought the bridge back through Termux:Boot (`scripts/termux-boot-chelus.sh`) 2 min 13 s after the reboot command, and sshd with it; wireless debugging did not come back (Android turns it off at boot). Not verified: that it answers a Telegram message after the reboot without anyone opening Termux, process survival and Doze over days, and missed-wake recovery.
- **Specific Telegram flows:** end-to-end approval-button interactions, newer skills flows, and memory-review/pending cards remain not independently verified in the live bridge.
- **Release parity:** the on-phone code (tool approvals, Telegram photos, skill size limits) is back in this repository as of 2026-10-28, and the phone has run deployed repository code since. Integration setup (AgentMail, Vercel, MEGA, ADB) lives only on the phone and is out of scope for 1.0.
- **Product site (2026-11-01):** `website/` builds the guides at clankclub.github.io/chelus with standard-library Python; GitHub Pages deploys it from `main`. Checked in a browser at desktop and phone widths: navigation, the mobile menu, copy buttons, no console errors, no sideways scroll. The phone photos are AI-generated and captioned as illustrative; real photos of the A10e would be better.
- **Portability:** the Galaxy A10e/32-bit Termux is the demonstrated platform. Additional hardware/OS support should be claimed only after testing.

Run the source test suite with `python -m unittest discover -s tests -t .`. Report separately what passed in desktop mocks, tests executed on the phone, and actual live network/device use. Avoid converting either self-reported field results or mocked tests into stronger guarantees than they support.

## Audit fixes (merged 2026-10-28)

Seven 2026-10-24 audit findings are fixed: Telegram batch tails, session ownership during slow retries, executable launcher and canonical install URL, bounded shell output, staged deployment with true test status, provider response validation, and guest listing metadata filtering. Covered by mocked tests, which run on the phone with every deploy.

## Consolidation and rename (2026-10-28)

`main` now joins three lines that had diverged: the GitHub docs, the audit fixes, and the code the phone was running, which had never been committed (Hermes's tool-approvals work plus edits made on the phone). The package, scripts and folders are renamed Chelys to Chelus; existing installs migrate their config and data folders on first start (see the README). The migration has run on the live install: its folders are `chelus`, with `chelys` symlinks left at the old paths.
