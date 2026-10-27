# Project status

Updated **2026-10-26**. This is a current-state summary, not a release announcement. Chelus is running on the owner's Samsung Galaxy A10e (Android 11, **32-bit Termux**) and is **pre-1.0**. Its core runs on Python's standard library without pip dependencies; model inference is remote.

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

- **Scheduling/cron:** base scheduling integration is still planned/in progress. Do not describe recurring unattended jobs as a completed built-in subsystem.
- **Unattended resilience:** Android process survival, extended background operation, reliable missed-wake recovery, and automatic restart after a reboot still need explicit end-to-end verification.
- **Specific Telegram flows:** end-to-end approval-button interactions, newer skills flows, and memory-review/pending cards remain not independently verified in the live bridge.
- **Release parity:** bring the latest on-phone code/integration setup back into this repository and document optional installation steps before presenting these external capabilities as reproducible from a fresh checkout.
- **Portability:** the Galaxy A10e/32-bit Termux is the demonstrated platform. Additional hardware/OS support should be claimed only after testing.

Run the source test suite with `python -m unittest discover -s tests -t .`. Report separately what passed in desktop mocks, tests executed on the phone, and actual live network/device use. Avoid converting either self-reported field results or mocked tests into stronger guarantees than they support.
