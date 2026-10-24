# Chelys

A small AI agent for 32-bit Android phones, running in Termux. The agent loop, tools, history and personality all live on the phone. Only the model call goes out to the cloud (DeepSeek).

Chelys is inspired by [Hermes Agent](https://github.com/NousResearch/hermes-agent) from Nous Research and aims to bring Hermes's behavior to phones that can't run it. Hermes's normal install fails on 32-bit ARM Android because several of its dependencies need Rust or C builds that nobody publishes for that platform, and the phone can't compile them. Chelys avoids that by using only the Python standard library. It is an independent project, not affiliated with Nous Research. See [NOTICE](NOTICE) for what comes from Hermes.

The name: *chelys* (KEL-iss) is the tortoise shell that baby Hermes turned into the first lyre. A slow, cheap shell made to sing.

**Status:** early. Terminal chat and a Telegram bot work on a real device (Samsung Galaxy A10e, Android 11, 32-bit Termux).

## Requirements

- Termux with `python` 3.11 or newer (tested on 3.14.8, armv8l). No pip packages.
- A DeepSeek API key.
- Optional, for `battery_status`: the `termux-api` package and the Termux:API app.

## Install

On the phone, in Termux:

```sh
git clone https://github.com/silascroe/chelys ~/chelys
ln -sf ~/chelys/chelys.sh $PREFIX/bin/chelys
```

Store the API key in a file only your Termux user can read:

```sh
mkdir -p ~/.config/chelys && chmod 700 ~/.config/chelys
read -rs DK            # paste the key; nothing is shown
(umask 077; printf '%s' "$DK" > ~/.config/chelys/deepseek.key); unset DK
```

`DEEPSEEK_API_KEY` in the environment overrides the file.

## Usage

```sh
chelys                      # chat, resuming the latest conversation
chelys chat --new           # chat in a fresh conversation
chelys chat --session 3
chelys ask "how's the battery?"   # one message, then exit
chelys sessions             # list conversations
chelys show [N]             # print a conversation
chelys usage                # token and cache-hit totals per conversation
chelys skills [ACTION]      # list, view, pending, approve, reject, undo, history, pin, revoke, curate
chelys telegram             # run the Telegram bot bridge (see below)
```

In chat: `/new`, `/learn [what]`, `/quit` and `/help`. Ctrl-C cancels the current reply; at the prompt it exits. If DeepSeek is still down after the normal retries, chat waits and tries again a few times with a countdown; Ctrl-C stops that too.

| Exit code | Meaning |
|---|---|
| 0 | ok |
| 1 | model/API error |
| 2 | config error |
| 130 | interrupted |

## Telegram

Chat with Chelys from Telegram. The phone polls Telegram over HTTPS, so it needs no public address and no open ports.

1. Create a bot with [@BotFather](https://t.me/BotFather) and get your numeric user ID, for example from @userinfobot.
2. Store both on the phone:

   ```sh
   read -rs TG            # paste the bot token; nothing is shown
   (umask 077; printf '%s' "$TG" > ~/.config/chelys/telegram.token); unset TG
   printf '%s' 123456789 > ~/.config/chelys/telegram_user_id   # your ID
   ```

3. Run the bridge:

   ```sh
   chelys telegram
   ```

The bot answers only your user ID in a private chat and drops everything else unread.

**Guests.** To let someone else chat with it, put their numeric user ID in `~/.config/chelys/telegram_guests`, one per line (`#` comments are fine), then restart the bridge. Guests get their own conversation and the safe tools. Risky tools are always refused for guests, and no approval buttons are shown. Remove the line and restart to revoke access. Guests use your DeepSeek key, and get no skills and no skill commands.

Risky tools ask for approval with a card in plain language: what the action is, Chelys's reason in her own words, and the facts (the command, or the file and its size). An unanswered request counts as Deny after 3 minutes. Replies list the tools used. `/new` starts a fresh conversation and `/help` shows the commands. The skill commands are `/learn [what]`, `/skills`, `/pending` and `/undo NAME` (see Skills below). If DeepSeek stays down after the normal retries, the bot posts a countdown and tries again up to 5 times, 15 to 60 seconds apart; `/stop` cancels the wait. The Telegram conversation is separate from terminal chat and survives restarts.

The bridge runs only while that Termux session is alive, and Android may kill Termux in the background. `termux-wake-lock` and turning off battery optimization for Termux help, but neither is a guarantee. Running unattended has not been tested yet.

To run it detached from an SSH session and log to a file:

```sh
(nohup setsid chelys telegram > ~/.local/share/chelys/telegram.log 2>&1 < /dev/null &)
pkill -f "[p]ython -m chelys telegram"     # stop it
```

## Tools

| Tool | Approval |
|---|---|
| `list_dir`, `read_file`, `battery_status` | none |
| `write_file`, `run_shell` | asks in chat; refused in `ask` unless you pass `--allow-risky` |
| `skills_list`, `skill_view` | none (owner only) |
| `skill_manage` | asks, with more choices (see Skills) |

`run_shell` runs commands in Termux with a timeout and kills the whole process group when time runs out. It is not a sandbox. The approval prompt is the safety.

File tools refuse the config folder, the data folder and `~/.ssh` for everyone, so the API key, bot token and history can't be read through them. Guests are limited further, to visible files under `workdir`.

## Skills

A skill is a saved procedure: a `SKILL.md` with a short frontmatter (`name`, `description`) and the steps, pitfalls and preferences for one kind of task, plus optional `references/`, `templates/`, `scripts/` and `assets/` files. Chelys sees a one-line index of her skills, loads the full text with `skill_view` when a task matches, and writes and fixes them with `skill_manage`. This is rebuilt from Hermes Agent's skills subsystem; [docs/hermes/skills.md](docs/hermes/skills.md) has the details and what differs.

Skills live in `~/.local/share/chelys/skills/<category>/<name>/`. You can write them by hand too.

**Approval.** Every skill write asks first, showing what changes as a diff. The answers are allow once, allow for this conversation, always for this skill (stored; `chelys skills revoke NAME` takes it back), and deny. A content scanner refuses text that looks like prompt injection, leaks secrets, or tampers with Chelys's config. Deleting a skill archives it, and `/undo NAME` reverts the last change to a skill.

**`/learn [what]`** asks Chelys to turn something into a skill: a file or folder, pasted text, or with nothing after it, what you just did together.

**Background review.** After every 10 tool-using steps in your conversation, Chelys reviews it once your reply is out and proposes skill changes from what happened: corrections you made, a fix that worked, a skill that turned out wrong. Proposals arrive as cards with Approve and Reject buttons, which don't expire. `/pending` shows them again. Up to 10 wait at a time. While a review runs, new messages wait for it to finish.

**Curator.** Once a week, when Chelys has been idle for 2 hours, skills that the review created and nobody used get marked stale after 14 days and archived after 30. Skills you made or asked for with `/learn`, and pinned ones (`chelys skills pin NAME`), are never touched.

## Configuration

`~/.config/chelys/config.toml` is optional. Unknown keys and wrong types are errors. The defaults:

```toml
base_url = "https://api.deepseek.com"
model = "deepseek-flash"      # DeepSeek renames models often; check their docs
thinking = false              # sends {"thinking": {"type": "disabled"}}
max_tokens = 2048
request_timeout = 60.0        # longest wait for any single read from the API
request_deadline = 300.0      # longest a whole model request may take
api_max_retries = 3           # transient errors (429, 5xx, timeouts) are retried with backoff
max_tokens_limit = 32768      # ceiling when a cut-off reply is retried with a bigger max_tokens
max_iterations = 8            # model steps per message
tool_output_chars = 6000
shell_timeout = 30.0
read_file_bytes = 65536
history_chars = 40000         # how much past conversation is sent each turn
compaction = true             # summarize older history instead of dropping it
autorecover_cycles = 5        # Telegram and chat: wait out an outage this many times; 0 disables
skill_approval = "ask"        # "off" lets skill writes through without asking
review_interval = 10          # tool-using steps between background skill reviews
curator = true                # weekly archive of unused review-made skills
workdir = "/data/data/com.termux/files/home"
```

The personality comes from `~/.config/chelys/persona.md` if it exists, otherwise from [chelys/persona_default.md](chelys/persona_default.md).

`CHELYS_CONFIG_DIR` and `CHELYS_DATA_DIR` move the config and data folders.

Files on the phone:

| Path | What |
|---|---|
| `~/chelys/` | the code |
| `~/.config/chelys/` | `deepseek.key`, plus `config.toml` and `persona.md` if you add them |
| `~/.local/share/chelys/history.sqlite3` | conversation history, skill revisions and pending changes |
| `~/.local/share/chelys/skills/` | skills (archived ones under `.archive/`) |

## Development

```sh
python -m unittest discover -s tests -t .
```

The tests use a fake model and a fake local HTTP server. They never call DeepSeek. The two shell tests are skipped on Windows.

`scripts/deploy.sh` copies a working copy to a phone over SSH (host alias `galaxy-a10e`, or set `CHELYS_HOST`) and runs the tests there.

## Design notes

**No SDK.** The `openai` package depends on Rust-built wheels (`pydantic-core`, `jiter`) that don't install on 32-bit Termux. DeepSeek's API is OpenAI-format JSON over HTTPS, so `urllib` covers it.

**Saved as it happens.** Each message is written to the database as the turn goes: the user message first, a tool request before its tool runs, each result as it arrives. A turn that is cancelled, fails, or dies with the phone is closed with a note saying which tools may already have run, so the history never pretends an action didn't happen.

**History is summarized, not dropped.** When the conversation would no longer fit in `history_chars`, the model summarizes the older middle into fixed sections (open request, goal, constraints, completed actions and so on), and the summary rides along with the first message after it. The first turn and the most recent turns stay word for word. Old rows are archived in the database, not deleted. If summarizing fails, nothing changes and it tries again 10 minutes later.

**One turn at a time.** The terminal and the Telegram bridge can share a conversation, but each turn takes a lease on it, and a second process gets a "busy" error instead of interleaving turns.

**DeepSeek quirks.** Both are handled in `provider.py`, following lessons Hermes recorded:

- Thinking off: `reasoning_content` is stripped.
- Thinking on: every assistant message must send it back, padded with `" "` when empty, or the API returns HTTP 400.
- An assistant tool-call message sends `null` content, not `""`.

## Uninstall

```sh
rm -rf ~/chelys ~/.local/share/chelys
rm -f $PREFIX/bin/chelys
rm -rf ~/.config/chelys   # also deletes the API key file
```

## License

MIT. See [LICENSE](LICENSE).
