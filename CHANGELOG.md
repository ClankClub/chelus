# Changelog

## 0.9.0 (2026-11-01): 1.0 release candidate

What 1.0 still needs is in [docs/STATUS.md](docs/STATUS.md#10-checklist).

- **Scheduling:** scheduled prompts and scripts (`chelus cron`, `/cron`, and the model's `cron` tool), delivered to Telegram, with catch-up after missed runs and script jobs that skip the model.
- **Tools:** `web_fetch` and `web_search`; shell commands up to 120 seconds, or in the background for up to 30 minutes with a finish notice; `search_files`; `edit_file` with the diff on the approval card; long output saved for `read_file` to page.
- **Notes:** a folder of linked Markdown notes, readable by Obsidian and similar apps, with six owner-only tools, link rewriting on rename, `/notes` and `chelus notes`, filing by the background review (edits wait for approval), and a weekly check for broken links.
- **Skills:** four starter skills in `skills/`, rewritten from Hermes Agent's bundled set.
- **Models:** any OpenAI-compatible API with `provider = "openai"`, besides DeepSeek.
- **Running unattended:** `scripts/termux-boot-chelus.sh` restarts the Telegram bridge after a reboot through Termux:Boot.
- **Telegram:** the bridge registers its commands, so `/` and the Menu button list them.
- **Test bench:** `bench/` runs scored conversations against other models in Docker on a PC.
- **Site:** guides at https://clankclub.github.io/chelus/, built from `website/`.

## 0.1.0

The first version: chat in the terminal and Telegram, tools with approvals, memory, skills, the background review and curator, and conversation history with compaction.
