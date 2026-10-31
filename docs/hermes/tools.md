# Hermes's tools vs what Chelus needs

The conclusion of a read of Hermes Agent's tools layer (NousResearch/hermes-agent @ 99fcd69, MIT): `tools/`, about 127,000 lines in roughly 350 files. Read in full or in their key parts: the file tools (`file_tools.py`, `file_operations.py` `patch_replace`, `fuzzy_match.py`, `file_tools_write_guards.py`, `file_state.py`), the web tools (`web_tools.py`, `web_tools_truncate.py`, `url_safety.py`, `website_policy.py`, the `ddgs` and `brave_free` backends), the terminal and background processes (`terminal_tool.py` schema, `process_registry.py`, `process_registry_notifications.py`), output limits (`tool_output_limits.py`, `tool_result_storage.py`, `budget_config.py`), and the headers of `transcription_tools.py`, `todo_tool.py`, `clarify_tool.py` and `checkpoint_manager.py`. Everything else was triaged by name and size. This file is the port list for tools; nothing on it is built yet.

## What Chelus has

Nine tools. `list_dir`, `read_file` (first 64 KB, no paging) and `battery_status` run freely. `write_file` (whole file) and `run_shell` (30 s, 64 KB) ask the owner first. `skills_*`, `memory`, `session_search` and `cron` are owner-only with their own checks. File tools refuse Chelus's config and data folders and `~/.ssh`, and guests see only non-hidden files under the work folder.

## What the phone actually does with them

The phone's history up to 2026-10-31, read-only: 432 `run_shell` calls against 13 `read_file`, 11 `write_file` and 36 `list_dir`. Of the shell calls:

- **105 fetch a URL.** DuckDuckGo's HTML page for search (17), r.jina.ai to turn a page into text (10), docs and GitHub pages, and authenticated APIs (Telegram, AgentMail, Vercel, GitHub). Many pipe through `head`, and some through `sed` chains that undo HTML entities by hand. Every one of them was an approval card.
- **163 use grep, rg or find**, many of them on curl output.
- **16 hit the 30 s timeout.**
- **About 18 edit a file**: 11 `write_file` calls plus 7 shell edits (`sed -i`, redirects).

The shell has become the tool for everything, so most of the owner's approval taps go to fetching web pages. That decides the order below, and it puts file patching lower than a first look at Hermes would.

## Hermes's design rules that carry over

- **A dedicated tool beats the shell.** Each Hermes tool description says what shell habit it replaces ("use this instead of cat/head/tail", "instead of grep/rg/find", "instead of sed/awk"). Dedicated tools give bounded output, structured errors, and no approval for reads.
- **Fetching is guarded by where it goes, not by asking.** `web_extract` doesn't ask the user. `url_safety.is_safe_url` resolves the host and refuses private, loopback, link-local, CGNAT, multicast and reserved addresses. It always refuses cloud metadata addresses, fails closed on DNS errors and odd URLs, and re-checks each redirect target. The httpx client also re-checks at connect time, which closes DNS rebinding.
- **Fetched text is data.** Pages come back as clean text with images as placeholders. A page over the character budget (default 15,000) becomes 75% head plus 25% tail cut on line boundaries. The full text is saved to a file, and a footer gives the exact `read_file` call that pages the middle.
- **Large output spills, it isn't cut.** Output over a tool's threshold is saved under a cache folder and replaced by a preview of about 1,500 characters plus the path. Chelus cuts at 6,000 characters and the rest is gone, which is why the model keeps piping into `head`.
- **Edits are find-and-replace with fuzzy matching.** `patch(path, old_string, new_string, replace_all)` must match exactly one place unless `replace_all` is set, returns a unified diff, keeps the file's CRLF and BOM, and treats an already-applied edit as success. Matching walks nine strategies in order: exact, line-trimmed, whitespace-collapsed, indentation-free, escape-normalized, boundary-trimmed, Unicode-normalized, then two similarity strategies (first and last line as anchors, middle scored). The two similarity strategies are refused under `replace_all` and flagged in the result, because their match boundaries can land a line off. Repeated misses get escalating hints.
- **Long commands go to the background with a completion notice.** `terminal(timeout=…)` defaults to 180 s. Past the foreground maximum, or with `background=true`, the command becomes a tracked process with a rolling output buffer. With `notify=true`, its completion is injected into the conversation as one message, and several completions are batched into one.

## Constraints that are Chelus's own

- **Standard library only.** `urllib`, `html.parser`, `difflib`, `ipaddress` and `socket` cover everything here. No httpx, no ddgs, no lxml (lxml is a pain on 32-bit Termux). `rg` and `ffmpeg` are installed on the phone but must stay optional.
- **One turn at a time.** The bridge blocks during a turn, so a 10-minute foreground command freezes Telegram. Long work belongs in the background, finishing through the same note path cron uses.
- **The approval gate is the safety.** Reads, searches and fetches are free. Anything that writes or runs still asks. A fetch tool that doesn't ask opens one new hole: a prompt-injected page could get the model to send what it has read to an outside URL in a GET query. The file tools already refuse the folders holding keys and history, which limits what can leak. The fetch tool adds a cap on URL length and refuses URLs with credential-named query parameters, which is Hermes's `sensitive_query_param_name` list. It carries no headers, cookies or auth, so authenticated APIs stay in the shell, behind approval.
- **Guests get none of these.** Web and search are owner-only, like skills and memory.
- **Every schema costs prompt tokens on every call.** DeepSeek caches the stable prefix, so a few more tools are cheap. A dozen more would not be. Prefer fewer tools with an `action`, and only where the history shows demand.

## Port list, in priority order

Sizes are estimates for Chelus. The whole list is about 900 lines.

| # | Status | What | Hermes source | Size | Why |
|---|---|---|---|---|---|
| 1 | todo | **`web_fetch` and `web_search`.** Fetch: http(s) GET only, at most 5 redirects, each hop re-checked. Every resolved address must be public: refuse private, loopback, link-local, CGNAT, multicast, reserved and metadata addresses, IPv4-mapped forms included. After connecting, check the socket's peer address too, which closes DNS rebinding without httpx. Download at most 2 MB, with a 20 s total deadline. Text, HTML, JSON and XML only. HTML goes to text with `html.parser` (drop script, style and nav; keep headings, list items and link targets). Head and tail cut at 12,000 characters, the full text saved for paging (item 3). Search: DuckDuckGo's HTML endpoint, keyless, as the phone already does, parsed into title, URL and snippet. A Brave Search key is used when one is configured, because the DuckDuckGo page breaks and rate-limits. Results are labeled as untrusted web content. Owner only, no approval. | web_tools.py, web_tools_truncate.py, url_safety.py, plugins/web/ddgs, brave_free | ~300 lines, new `web.py` | A quarter of all shell calls, each one an approval tap, with HTML cleaned by sed. |
| 2 | todo | **Shell timeout and background runs.** `run_shell` gets `timeout`, default 30 s and at most 120 s in the foreground. With `background=true` the command runs detached in its own process group, its output goes to a log file capped at 256 KB, and a tool result with a job id comes back at once. When the bridge's housekeeping sees it finish, it sends the owner one line (exit code, last lines of output), with a message id required as for cron. It also stores a note so the next turn sees the result. A background run has a 30-minute limit. Still risky: the approval card says it will run in the background. One `shell_jobs` action (list, log, kill) is owner-only. | terminal_tool.py schema, process_registry.py, process_registry_notifications.py | ~200 lines | 16 timeouts so far, and Vercel deploys and package installs don't fit in 30 s. Reuses cron's delivery and note path. |
| 3 | todo | **Spill instead of cut, and page with `read_file`.** Tool output past the cap is saved to `~/.cache/chelus/spill/<id>.txt` (0700, pruned after 24 h). The model gets the first ~1,500 characters, the size, and the `read_file` call that pages it. `read_file` gains `offset` and `limit` (lines, at most 2,000 and 60,000 characters per call) and line numbers, and the spill folder is readable even though the data folder is not. Spilled shell output goes through the same redaction as script output. | tool_result_storage.py, budget_config.py, file_tools.py `read_file`, web_tools_truncate.py | ~120 lines | The model pipes into `head` because past 6,000 characters the rest is lost. Item 1 needs it for long pages. |
| 4 | todo | **`search_files`.** Content search by regex or filename search by glob, under a path, with `limit`, `context` and `files_only`. Uses `rg` when it's installed, otherwise a Python walk with a file-size cap and binary skip. It never enters or reports the off-limits folders: the search root is checked, `rg` gets exclusion globs, and every result path is checked again before it is shown. Guests are kept to the work folder like the other file tools. No approval. | file_tools.py `search_tool`, file_operations_search.py | ~120 lines | 163 shell calls grep or find. The ones on files become free and bounded. |
| 5 | todo | **`edit_file` (find and replace).** `path`, `old_string`, `new_string`, `replace_all`. Uses the seven normalizing strategies, not the two similarity ones: a phone owner approving a diff shouldn't have to spot a line-off boundary. The match must be unique unless `replace_all` is set. CRLF and BOM are kept. An already-applied edit is a no-op success. A miss names the closest lines, and three misses on one file suggest re-reading it. The approval card shows the unified diff instead of the first 300 characters of a whole file, and the fingerprint for "Always" grants covers path plus diff. `write_file` gains a hint when it re-sends a mostly unchanged file. | fuzzy_match.py, file_operations.py `patch_replace`, file_tools.py `patch_tool` | ~180 lines | Few edits so far (about 18), so it isn't urgent. When an edit does happen, the owner sees exactly what changes, and long files stop being re-sent whole. |
| 6 | owner's call | **Telegram voice notes.** Download the voice file, send it to an OpenAI-compatible transcription API (Groq's Whisper accepts Telegram's OGG/Opus directly; multipart built with the standard library), and treat the transcript as the owner's message, shown back as `[voice] …`. Needs a key in the config. Without one, the bridge says voice needs a key. Local whisper.cpp on the A10e is possible but slow, and is not planned. | transcription_tools.py, transcription_cloud.py | ~100 lines | Nothing in the history asks for it. Worth it only if talking to Chelus beats typing for you. |

## Not ported, and why

- **Subagents and delegation, browser automation, MCP, computer use, code kernels.** Too much for a 2 GB phone, and the shell covers the rest.
- **Todo list.** The 8-step cap ends a turn before a plan would help.
- **Clarify.** The model can ask in its reply and end the turn. Approval cards already cover yes/no.
- **`send_message`.** Cron and the background-run notice deliver. The model talking to other chats is not wanted.
- **Checkpoints (a shadow git of the work folder).** Every write is approved, and item 5 shows a diff. A one-file `.bak` of the last overwrite can come later if a bad write ever happens.
- **Read dedup, stale-write refusal and cross-agent file state.** These guard parallel subagents and models that loop. Chelus has one agent, the repetition guard, and an approval on every write.
- **Write guards for protected instruction files and sensitive system paths.** Every write already asks. The refusals that matter (keys, history, `~/.ssh`) are hard limits already.
- **Website blocklist, keyless rescue chains, result caches, a dozen search vendors, PDF and Office extraction, TTS, image and video generation, vision.** Photos already reach the model through Telegram. The rest is a service menu for many users.
- **Linting after writes.** Useful for coding agents. Chelus mostly edits notes, skills and small scripts. Can come with item 5 later if code edits become common.

## Order

Items 1 and 3 go together: a fetch tool without spill loses long pages. Then 2, which reuses cron's delivery. 4 and 5 are independent and small. 6 waits for your call.
