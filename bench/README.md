# Test bench

Chelus in a Docker container on a PC: try other models, talk to a test bot, and run scored conversations. The repo is mounted read-only, so code edits take effect on the next run and the agent can't edit itself. Each model profile gets its own home in a Docker volume (SQLite and Windows bind mounts don't mix), so the bench never touches the phone's memory or skills.

What it can't tell you: anything about the phone itself. 32-bit Termux, the slow CPU, Doze and reboots still need the real device.

## Setup

Docker, plus a key in an environment variable the profile names. Compose passes `OPENROUTER_API_KEY` and `DEEPSEEK_API_KEY` through from the host; nothing is written to disk.

Money: `deepseek-flash` talks to DeepSeek directly on its own key, with the phone's exact settings (thinking on, effort low). OpenRouter is for the cheap non-DeepSeek models; each profile notes its price. The cheapest are GPT-OSS 120B (cheaper than the 20B) and Gemma 4 26B on the free tier.

```sh
docker compose -f bench/compose.yaml build
```

## Use

`CHELUS_PROFILE` picks a file from `models/` (default `gpt-oss-20b`).

```sh
docker compose -f bench/compose.yaml run --rm chelus help
CHELUS_PROFILE=gpt-oss-20b docker compose -f bench/compose.yaml run --rm chelus chat
docker compose -f bench/compose.yaml run --rm chelus sim --models gpt-oss-20b,deepseek-flash
```

**Test bot.** Make a second bot with BotFather and put its token in `secrets/telegram.token` and your numeric Telegram ID in `secrets/telegram_user_id` (the folder is git-ignored). Then `CHELUS_PROFILE=... docker compose -f bench/compose.yaml up -d bot`; switch models by restarting it with another profile. Only one process may poll a bot token, so don't point the bench at the phone's bot.

**Local models.** `models/local.toml` points at `llama-server` on the Windows host (`--jinja` is needed for tool calls). Put any text in the key it names, e.g. `LOCAL_API_KEY=x`.

## Simulations

`python -m bench.sim` (the `sim` command above) runs each scenario in `scenarios/` against each model, in a new home every time, and writes a transcript, a JSON record and a summary table to `out/<timestamp>/`. Nothing deletes earlier results: transcripts stay in `out/` (git-ignored, so `git clean -x` would remove them), and each run's home, with its history database and any files the agent changed, stays in the volume under `/state/runs/<timestamp>/`.

```
sim --models a,b --scenarios all|name,name --repeat N --owner gpt-oss-120b --judge deepseek-flash|none --max-cost 0.25
```

A scenario is TOML:

- `turns`: scripted messages. `/new` starts a new conversation; `/wait N` sleeps N seconds (for background jobs).
- `[owner]` with `persona` and `goal`: after the script runs out, a model plays the owner until it says `[DONE]` or `max_turns` is reached.
- `role = "guest"`, `approve = "once"|"session"|"always"|"deny"` (approval cards are answered this way), `[settings]` overrides, `[[seed]]` files in the workdir, `[memory]` entries, `starter_skills = false`, `review = true` (run the background review after a reply when it's due, as the Telegram bridge does; set `memory_review_interval` in `[settings]` to make it due sooner).
- `[[check]]`: `tool_used`, `tool_not_used`, `skill_viewed`, `reply_regex`, `reply_not_regex`, `args_not_regex`, `file_regex`, `file_missing`, `review_regex` (what the background review reported), `max_calls`, `no_errors`, and `judge` (a criterion a model grades from the transcript).

Costs: OpenRouter reports each call's charge; with bring-your-own-key routing the real charge is `cost_details.upstream_inference_cost`, which the simulator uses. DeepSeek reports none, so its cost is estimated from `prices.toml` at peak rates (marked `~`; off-peak is half). The simulated owner and the judge are totalled separately. `--max-cost` (default $0.25) stops starting new runs once the whole invocation has spent that much. One full suite (eight scenarios) cost about $0.0015 on GPT-OSS 20B and $0.04 on DeepSeek Flash on 2026-11-01.

The judge is a cheap model and can be wrong, and DeepSeek judging DeepSeek may be lenient, so read the transcript before trusting a judged result.
