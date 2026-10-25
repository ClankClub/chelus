# Project status

Evidence recorded on 2026-10-24. Update this page **in place** when testing changes; old pass counts are not current guarantees. Setup and commands belong in [README.md](../README.md).

## Verified live on the target phone

- Samsung Galaxy A10e, Android 11, 32-bit Termux: terminal DeepSeek chat, model-selected battery tool, conversation resume, and basic error/cancel behavior.
- The Telegram bridge handled an owner conversation.
- Owner memory survived a new conversation, and session search retrieved prior conversation content with DeepSeek.

## Implemented and covered by automated tests

- Telegram guest isolation and risky-tool refusal; skills, approval handling, review, and curation. Tests used mocked external services, including when run on the phone.
- History persistence, compaction, request recovery, and turn leasing. Not every path has had an independent live-model test.
- Runtime telemetry footer and optional read-only ADB cache reader. Android sensor access varies by device; the sampler is started manually.

## Still unverified

- End-to-end Telegram approval-button interactions, newer skills workflows in the live bridge, and memory-review/pending cards.
- Reliable unattended operation, Android process survival in the background, and automatic recovery after reboot.

Run the full suite with `python -m unittest discover -s tests -t .`. Report separately what passed in desktop mocks, phone tests, and live network/device use.
