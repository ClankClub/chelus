# Current status

Updated by Codex, 2026-10-24.

Chelys is an early personal-device agent. Terminal chat and Telegram have prior repository-reported live use on a Galaxy A10e/Android 11, using Termux Python 3.14.8 armv8l and DeepSeek. This branch has not been run on that phone.

## Audit fixes

Branch: `fix/audit-reliability-2026-10-24`, based on `363b041ead60d5d32bdd9cb0cb12b4696c77a138` (including the upstream compaction budget fix and memory/session-search subsystem).

The seven 2026-10-24 findings are addressed: Telegram batch tails, session ownership during slow retries, executable launcher/canonical installation URL, bounded shell output, staged deployment with true test status, provider response validation and guest listing metadata filtering. This branch changes no pip dependencies and performs no device deployment.

Local verification: Python 3.12.14/Linux; the earlier pre-memory baseline passed 177 tests and the audit fixes passed 199 tests. After integrating the memory port, all 232 tests passed. The tests and targeted regressions use fake model/Telegram responses, local SQLite and real local subprocesses. Launcher help was exercised through sh and its Git executable mode verified. Deployment tests use a local fake SSH transport and verify failed candidates do not replace the install.

## Device evidence and remaining checks

Prior repository evidence reports live chat, autonomous battery-tool selection, session resume, missing/rejected keys, timeout/Ctrl-C handling, early retry/cache behavior and Telegram owner chat. The telemetry commit `a79f6d8` reports 172 tests passing on the phone; that is earlier code and is not verification of these fixes.

Still required on the updated code: real approval-button interactions, provider outages spanning long retries, guest file boundaries, shell overflow/timeout on Termux, deployment failure/success, and restart during a tool turn. Overnight operation, reboot survival and Android background process killing remain unestablished.

The memory-port commit reports 209 tests on the PC and phone, plus live cross-conversation memory recall and session search. Memory review and pending-memory cards were not reported live-tested. This branch preserves that subsystem; those device claims were not repeated here.

## Scope

Hermes turn-loop and skill behavior is implemented. Memory and session search were ported upstream in `363b041`; reading/port notes are in `docs/hermes/memory.md`. Existing GitHub issues #1 (Telegram Markdown rendering) and #2 (tool-iteration limits) are separate from the seven audit fixes. GitHub CI has not been added by this branch.
