# Local inference: the local profile

**Experimental.** Chelus normally sends every model call to DeepSeek or another API. With `profile = "local"` it talks to a small model running on the phone itself, through llama.cpp's `llama-server`. Nothing leaves the phone, and nothing costs money, but the model is tiny and slow: treat it as a fallback that can read files, run approved commands and answer simple questions, not as a replacement for the cloud model.

The full profile is unchanged and stays the default. Switching back is one line in `config.toml`.

## What changes

A 0.8B model drowns in Chelus's full prompt (about 7,000 tokens of instructions and 30 tool schemas), and a phone reads prompt text at about 9 tokens a second. The local profile cuts the first request to about 730 tokens:

- **Five tools:** `list_dir`, `read_file`, `write_file`, `run_shell` and `battery_status`, with short schemas. `write_file` and `run_shell` still go through the approval card, the same as on the full profile; the file tools keep their limits.
- **A short prompt:** [persona_local.md](../chelus/persona_local.md) plus one line about approvals (and one for guests). Put your own in `~/.config/chelus/persona-local.md`. `persona.md` is not used.
- **Off:** skills, memory, notes, web, scheduling tools, session search, the background reviews and compaction. `/learn` says it needs the full profile.
- **`read_file`** puts a `[path, N lines]` line in front of a whole file, which kept the small model from confusing one file with another (10 of 16 runs right instead of 3 in one tuning test).

Settings the profile changes, unless `config.toml` sets them itself (the list is `LOCAL_DEFAULTS` in [config.py](../chelus/config.py)):

| Setting | Local | Why |
|---|---|---|
| `provider`, `base_url`, `model` | `openai`, `http://127.0.0.1:8081/v1`, `local` | llama-server on the phone |
| API key | none needed | `local.key` is used if it exists (llama-server `--api-key`) |
| `thinking` | `true` | see below; sent as llama-server's `enable_thinking` |
| `temperature` | 0.6 with thinking, 0.2 without | Qwen's advice for thinking; low was best without it |
| `max_tokens` | 1536 | room to think and still call a tool |
| `request_timeout`, `request_deadline` | 900 s, 1800 s | a slow phone is not an outage |
| `api_max_retries`, `autorecover_cycles` | 1, 0 | retrying a stuck local server rarely helps |
| `compaction` | `false` | a summary call would cost minutes |
| `max_iterations` | 6 | |
| `tool_output_chars`, `history_chars` | 2000, 8000 | prompt reading is the slow part |

## Setup

Chelus doesn't ship llama.cpp or a model. What was tested on the Galaxy A10e (32-bit Termux):

- **llama.cpp b11804**, cross-compiled for armeabi-v7a (NEON-VFPv4) with the Android NDK, static libc++, OpenMP off. Termux's own `llama-cpp` package might work too; it hasn't been tried.
- **Qwen3.5-0.8B, Q4_0** (490 MB), converted and quantized from the official weights.

Start the server, with 4 threads (8 threads got Termux killed by Android's low-memory killer on the A10e):

```sh
llama-server -m Qwen3.5-0.8B-Q4_0.gguf -t 4 -c 8192 -np 1 --host 127.0.0.1 --port 8081 --jinja --cache-reuse 256
```

`--jinja` is required for tool calls. `-np 1` keeps one slot, so every turn reuses the same prompt cache. Then in `~/.config/chelus/config.toml`:

```toml
profile = "local"
```

## How well it works

Qwen3.5-0.8B on the bench (`bench/scenarios/local-*.toml`, llama-server on a PC running the phone's model file, so the answers match the phone's but not its speed). Six scenarios were used to tune the prompt and schemas; three (`local-config`, `local-folder`, `local-count`) were kept back to check the tuning:

| | Tuned scenarios | Held-out scenarios | Output per model call |
|---|---|---|---|
| Thinking off | 95 of 100 checks | 15 of 50 | about 27 tokens |
| Thinking on | 49 of 60 | 39 of 60 | about 160 to 190 tokens |

Without thinking, the tuning had mostly fit the test questions. Thinking on is the default because the held-out column is the better guess at real use; with three repeats per scenario the numbers are rough. `thinking = false` is about twice as fast per turn.

**On the A10e (2026-11-19),** live through `chelus ask` with 4 threads: reading a named file answered correctly in 160 s from a cold start (81 s reading the 747-token prompt, 41 s thinking and calling the tool, then the answer). The next turn in the same conversation took 37 s, because llama-server only read the 136 new tokens; the cached prompt also carries over to new conversations while the server runs. A held-out question (a port in a config file) was answered right in 69 s. A refused `write_file` was not attempted again, and an allowed one wrote the right file in two of three tries (once answering with the list instead of writing it).

Typical mistakes: wrong answers about a file it has just read (asked which list item was about the bus, it named the batteries), mangled file names (`todo.txt` read as `todos.txt`), guessed absolute paths, replies that echo the message's timestamp, and saying it can't read files instead of trying.

**Speed:** about 9 tokens a second reading the prompt and 4 writing. Most of a turn's time is the model thinking; a cold first request adds about 80 s.

## Not yet done

- **Prompt cache across server restarts:** llama-server keeps the cached prompt only while it runs. Saving and restoring the slot (`--slot-save-path`) would make the first turn after a restart cheap.
- **Any API key provider in the setup flow:** the full profile already talks to any OpenAI-compatible API (see the README); there's no guided setup for it.
