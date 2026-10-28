# Chelus

<p align="center">
  <img src="assets/chelus-header.svg" alt="Chelus — 32-bit AI agent for Termux" width="820">
</p>

> Chelus is a small, persistent AI agent built to do more with less. It runs on a Samsung Galaxy A10e using 32-bit Android/Termux, in an environment too constrained for many modern agent stacks.

The core agent loop, tools, conversation history, memory, skills, and Telegram bridge run **on the phone**, using Python's **standard library only** (no pip dependencies). Model inference happens remotely through DeepSeek; optional services and command-line tools can extend the agent without becoming core dependencies. The goal is a capable, inspectable agent for constrained computers—not a model running inference locally, and not a 32-bit-only Python codebase.

The project adapts ideas from [Hermes Agent](https://github.com/NousResearch/hermes-agent) for hardware that cannot install its usual dependencies. Chelus is independent of Nous Research. See [NOTICE](NOTICE) for attribution.

**Status (2026-10-26):** operational, **pre-1.0**, on a Samsung Galaxy A10e (Android 11, 32-bit Termux). The owner has confirmed live operation beyond terminal/Telegram chat: email, deployments through an on-phone Vercel CLI, hosted job storage, wireless ADB operations, and external research/storage tools. **The live setup is ahead of the checked-in source**; those optional integrations are not all supplied by a fresh clone. Base scheduling/cron integration and unattended/reboot reliability still need work. See [current verification and remaining gaps](docs/STATUS.md).

**Project name vs. command name:** the repository is now **Chelus**, but the Python package, `chelys` command, script names, and local config/data paths have not yet been renamed. Commands below use the identifiers that work with the current code.

[Requirements](#requirements) · [Install](#install) · [Terminal](#terminal-usage) · [Telegram](#telegram) · [Development](#development)

## Requirements

- [Termux](https://termux.dev/) with Python 3.11 or newer (tested with 3.14.8 on armv8l).
- A DeepSeek API key for model inference.
- Optional for the `battery_status` tool: the Termux `termux-api` package and the Termux:API app.

**Node.js is not required to run Chelus.** It is installed separately on the owner's phone to support optional tooling, including the Vercel CLI. Likewise, cloud accounts, ADB, and other external CLIs/APIs are enhancements, not runtime dependencies. Other Linux/Python environments are a portability goal, not yet a tested support matrix.

## Install

Run in Termux:

```sh
git clone https://github.com/ClankClub/chelus.git ~/chelys
ln -sf ~/chelys/chelys.sh "$PREFIX/bin/chelys"
```

Store your DeepSeek API key in a user-readable-only file:

```sh
mkdir -p ~/.config/chelys && chmod 700 ~/.config/chelys
read -rs DK  # paste the key, then press Enter
(umask 077; printf '%s' "$DK" > ~/.config/chelys/deepseek.key); unset DK
```

Alternatively, set `DEEPSEEK_API_KEY` in the environment; it takes precedence over the key file.

## Terminal usage

```sh
chelys                          # chat; resume the latest conversation
chelys chat --new               # start a new conversation
chelys chat --session 3         # resume a particular conversation
chelys ask "how's the battery?" # one response, then exit
chelys sessions                 # list conversations
chelys show 3                   # inspect a conversation
chelys usage                    # token and cache-hit totals
chelys skills                   # list saved skills
chelys skills --help            # skill management commands
chelys memory                   # show persistent memory
chelys memory forget user 2     # forget entry 2 in the user profile
chelys telegram                 # start the Telegram bridge
```

In terminal chat, use `/new`, `/learn [what]`, `/memory`, `/help`, or `/quit`. Ctrl-C cancels a reply; at the prompt, it exits. If DeepSeek has an outage, Chelus retries with a countdown.

Exit codes: `0` success, `1` model/API error, `2` configuration error, `130` interrupted.

## Telegram

Chelus polls Telegram over HTTPS. It needs no public IP address or open ports.

1. Create a bot with [@BotFather](https://t.me/BotFather) and find your numeric Telegram user ID (for example, with @userinfobot).
2. Save the bot token and your user ID on the phone:

   ```sh
   read -rs TG  # paste the bot token, then press Enter
   (umask 077; printf '%s' "$TG" > ~/.config/chelys/telegram.token); unset TG
   printf '%s' 123456789 > ~/.config/chelys/telegram_user_id  # replace with your user ID
   ```

3. Start it with `chelys telegram`.

The bot accepts private messages from the configured owner. For guest access, put additional numeric user IDs in `~/.config/chelys/telegram_guests`, one per line, and restart the bridge. Guests have separate conversations, cannot access skills or memory, and cannot run risky tools. Guest requests still use your DeepSeek account.

Useful Telegram commands include `/new`, `/learn`, `/skills`, `/pending`, `/undo NAME`, `/memory`, `/stop`, and `/help`. Risky actions require an approval card; unanswered requests are denied after three minutes. If the API stays unavailable, `/stop` cancels the retry countdown.

### Runtime stats

Telegram replies include a small runtime footer by default, when the readings are available:

```text
mem 1.2/1.7G | cpu 3.9% - 30°C | bat 89% - 27°C
```

`/stats` toggles it for that chat, `/stats on|off` sets it, and `/stats info` explains the readings and their sources. Use `/stats add swap up disk`, `/stats remove cpu`, or `/stats reset` to change fields. Preferences survive bridge restarts. These stats are added outside the model context and conversation history.

Android may block direct CPU and thermal readings. An optional, **manually started** ADB-side helper at [scripts/chelys-telemetry.sh](scripts/chelys-telemetry.sh) can supply those readings without root. From a computer with authorized ADB access:

```sh
adb push scripts/chelys-telemetry.sh /data/local/tmp/chelys-telemetry.sh
adb shell 'chmod 700 /data/local/tmp/chelys-telemetry.sh; nohup setsid /system/bin/sh /data/local/tmp/chelys-telemetry.sh > /data/local/tmp/chelys-telemetry.log 2>&1 < /dev/null &'
```

This helper reads metrics only, starts no service, and must be restarted after a phone reboot. Chelus itself does not launch ADB or request privileged access for telemetry. Unavailable readings are omitted rather than invented.

**Background reliability is not guaranteed.** Android can kill Termux. A wake lock and disabling battery optimization may help, but unattended operation has not been verified. To detach the Telegram bridge from an SSH session:

```sh
(nohup setsid chelys telegram > ~/.local/share/chelys/telegram.log 2>&1 < /dev/null &)
```

## Tools, memory, and skills

Chelus can list and read files, check the battery, write files, and run shell commands. File writes and shell commands require approval in chat; `chelys ask` refuses them unless you explicitly pass `--allow-risky`. **Shell access is not sandboxed.** File tools refuse the config and data directories and `~/.ssh`; guests are further confined to the configured `workdir`.

**Memory:** Chelus can keep small, owner-only notes across conversations. It can save facts directly and shows what was saved; proposed changes from background review await approval. Inspect or delete entries with `chelys memory` (or `/memory` in Telegram). With SQLite FTS5 available, the agent can also search past conversations.

**Skills:** A skill is a saved `SKILL.md` procedure, optionally accompanied by reference files or scripts. Use `/learn [what]` to create one from instructions, files, or a recent workflow. Skill writes require approval by default; `chelys skills` manages pending changes, revisions, pins, and undo. Review may propose changes after a conversation, and the weekly curator archives unused review-created skills (not skills you created or pinned). See [Hermes skills notes](docs/hermes/skills.md) for implementation details.

`run_shell` captures at most 65,536 bytes across stdout and stderr; past that, or on timeout or Ctrl-C, it stops the whole process group and returns bounded partial output. Directory listings apply the file-tool policy to each entry, including symlink targets.

**Ordinary tool approvals.** In chat and Telegram, `run_shell` and `write_file` ask by default. **Allow once** approves only that call; **Allow for this conversation** covers both ordinary risky tools in that conversation, including later messages and after a restart. `/new` asks again. **Always this action** remembers only the exact tool, normalized real working directory and target, and effect arguments (the complete command, or file path and content; shell timeout is included). Changing those asks again; changing only Chelys's explanation does not. This is not a command-prefix or global execute-all grant. Deny, unanswered prompts and allow-once do not save grants. Guests can neither use nor manage grants. Skill approvals remain separate.

List ordinary grants with `/approvals` in chat or Telegram, or `chelys approvals` in the terminal. Use `/approvals revoke ID` or `/approvals clear` (terminal: `chelys approvals revoke ID` / `chelys approvals clear`). Listings show the grant ID, scope and tool/fingerprint, not stored commands or file contents. `ask` never reuses these grants: `--allow-risky` is still required explicitly.

Conversation history, memories, and skills are stored locally under `~/.local/share/chelys/`; credentials and optional configuration live under `~/.config/chelys/`. The Telegram owner and guests do not share conversation history.

## Beyond the base installation

The owner's **live Galaxy A10e installation** has demonstrated capabilities beyond the currently documented base checkout:

- **Email:** sends, reads, and replies using an AgentMail HTTPS API without a local mail client; a message from Chelus reached an external recipient on 2026-10-26.
- **Deployment:** runs the Vercel CLI **on the phone** (with separately installed Node.js), creating preview and production deployments. Vercel's remote infrastructure performs the application build, not the phone. One deployed example is the owner-restricted [Chelus Brainstem](https://chelus-brainstem.vercel.app/) job queue.
- **Durable hosted jobs:** that deployment uses Upstash Redis for state that survives the phone sleeping or going offline. Redis and the application run off-device; the agent runtime remains on the phone.
- **Device operations:** uses wireless ADB for device and package inspection, including removing and restoring packages in the owner's setup.
- **External resources:** uses web search/scraping and MEGA cloud storage, in addition to its local procedural skills.

These are **owner-confirmed results from one configured installation**, not claims that every item ships with the Python core or has been reproduced by a clean install from GitHub. Some live changes and integration setup have not yet been reflected in the repository source. Optional tools can depend on additional packages, native binaries, credentials, and external services even though **Chelus itself remains standard-library Python**. The operational evidence and verification boundaries are tracked in [docs/STATUS.md](docs/STATUS.md).

## Configuration

Optional settings go in `~/.config/chelys/config.toml`. For example:

```toml
model = "deepseek-flash"
thinking = false
reasoning_effort = "high"   # low, high or max; sent only when thinking = true
history_chars = 40000
max_iterations = 8
compaction = true
skill_approval = "ask"
review_interval = 10
memory = true
memory_review_interval = 10
session_search = true
curator = true
```

The defaults and full set of supported keys are in [chelys/config.py](chelys/config.py); unknown keys or wrong types are errors. `CHELYS_CONFIG_DIR` and `CHELYS_DATA_DIR` override the respective directories. Customize the agent's personality with `~/.config/chelys/persona.md`; otherwise it uses [the default persona](chelys/persona_default.md).

## Development

```sh
python -m unittest discover -s tests -t .
```

Tests use a fake model and local HTTP server; they do not call DeepSeek.

`sh scripts/deploy.sh` stages a candidate in a temporary folder on the phone (host alias `galaxy-a10e`, or set `CHELYS_HOST`), runs the suite there and reports the real test status. A failing candidate leaves the install untouched; a passing one is copied into place. Stop the bridge before deploying and restart it afterward.

Chelus sends OpenAI-compatible JSON to DeepSeek using `urllib` instead of an SDK with native dependencies. Conversations are saved as they happen, and older context is summarized rather than silently dropped. Only one turn can run against a conversation at a time. Design and porting notes live in [docs/hermes/](docs/hermes/).

## Uninstall

```sh
rm -f "$PREFIX/bin/chelys"
rm -rf ~/chelys ~/.local/share/chelys
rm -rf ~/.config/chelys  # also deletes saved credentials and custom settings
```

## License

MIT. See [LICENSE](LICENSE).
