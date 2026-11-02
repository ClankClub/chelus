#!/data/data/com.termux/files/usr/bin/sh
# Launcher: runs the agent from wherever this folder lives. Symlink it into $PREFIX/bin as "chelus".
here="$(dirname "$(readlink -f "$0")")"
# Owner-written exports for your own integrations (AGENTMAIL_API_KEY and the like), so the bridge
# has them however it was started, Termux:Boot included. See "Keys for your own integrations" in the README.
conf="${CHELUS_CONFIG_DIR:-${CHELYS_CONFIG_DIR:-$HOME/.config/chelus}}"
[ -f "$conf/env" ] && . "$conf/env"
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec python -m chelus "$@"
