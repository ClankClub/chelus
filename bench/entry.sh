#!/bin/sh
# Bench entry point. "sim ..." runs simulations; anything else runs the chelus CLI as the
# agent for $CHELUS_PROFILE (a file in bench/models/), with its own home under /state.
set -eu
cmd=${1:-help}
if [ "$cmd" = sim ]; then
    shift
    exec python -m bench.sim "$@"
fi
if [ "$cmd" = help ]; then
    echo "usage: chat | ask TEXT | telegram | sim [--models ...] [--scenarios ...] | any chelus command"
    echo "profiles (CHELUS_PROFILE):"; ls /app/bench/models | sed 's/\.toml$//; s/^/  /'
    exit 0
fi
profile=${CHELUS_PROFILE:-gpt-oss-20b}
toml=/app/bench/models/$profile.toml
[ -f "$toml" ] || { echo "no profile $profile in bench/models" >&2; exit 2; }
export HOME=/state/agents/$profile
cfg=$HOME/.config/chelus
mkdir -p "$cfg" "$HOME/.local/share/chelus/skills"
umask 077
grep -v '^\s*#' "$toml" > "$cfg/config.toml"
for f in telegram.token telegram_user_id telegram_guests; do
    if [ -f "/secrets/$f" ]; then cp "/secrets/$f" "$cfg/$f"; fi
done
# Starter skills, once; after that the agent owns its skills folder.
if [ ! -e "$HOME/.local/share/chelus/skills/.seeded" ]; then
    cp -r /app/skills/. "$HOME/.local/share/chelus/skills/"
    touch "$HOME/.local/share/chelus/skills/.seeded"
fi
cd "$HOME"
exec python -m chelus "$@"
