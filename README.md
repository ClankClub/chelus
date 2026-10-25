# Chelus

<p align="center">
  <img src="assets/chelus-header.svg" alt="Chelus — 32-bit AI agent for Termux" width="820">
</p>

> Chelus is a 32-bit AI agent built to do more with less. Running in Termux on vanilla Python, it calls a crowded Galaxy A10e home.

Its agent loop, tools, conversation history, memory, skills, and Telegram bridge all run on the phone. Only model requests go to DeepSeek. The local runtime uses Python's standard library, without pip dependencies.

The project adapts ideas from [Hermes Agent](https://github.com/NousResearch/hermes-agent) for hardware that cannot install its usual dependencies. Chelus is independent of Nous Research. See [NOTICE](NOTICE) for attribution.

**Status:** early, running on a Samsung Galaxy A10e (Android 11, 32-bit Termux). Terminal chat, owner Telegram chat, memory, and conversation search have been exercised live. Skills have automated coverage, but end-to-end Telegram use and unattended/reboot operation remain unverified. See [current verification](docs/STATUS.md).

**Project name vs. command name:** the repository is now **Chelus**, but the Python package, `chelys` command, script names, and local config/data paths have not yet been renamed. Commands below use the identifiers that work with the current code.

[Requirements](#requirements) · [Install](#install) · [Terminal](#terminal-usage) · [Telegram](#telegram) · [Development](#development)

## Requirements

- [Termux](https://termux.dev/) with Python 3.11 or newer (tested with 3.14.8 on armv8l).
- A DeepSeek API key.
- Optional for the `battery_status` tool: the Termux `termux-api` package and the Termux:API app.

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

Conversation history, memories, and skills are stored locally under `~/.local/share/chelys/`; credentials and optional configuration live under `~/.config/chelys/`. The Telegram owner and guests do not share conversation history.

## Configuration

Optional settings go in `~/.config/chelys/config.toml`. For example:

```toml
model = "deepseek-flash"
thinking = false
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

Tests use a fake model and local HTTP server; they do not call DeepSeek. The [deployment script](scripts/deploy.sh) copies a checkout to the target phone via SSH and runs tests there.

Chelus sends OpenAI-compatible JSON to DeepSeek using `urllib` instead of an SDK with native dependencies. Conversations are saved as they happen, and older context is summarized rather than silently dropped. Only one turn can run against a conversation at a time. Design and porting notes live in [docs/hermes/](docs/hermes/).

## Uninstall

```sh
rm -f "$PREFIX/bin/chelys"
rm -rf ~/chelys ~/.local/share/chelys
rm -rf ~/.config/chelys  # also deletes saved credentials and custom settings
```

## License

MIT. See [LICENSE](LICENSE).
