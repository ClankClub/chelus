#!/data/data/com.termux/files/usr/bin/sh
# Launcher: runs the agent from wherever this folder lives. Symlink it into $PREFIX/bin as "chelys".
here="$(dirname "$(readlink -f "$0")")"
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec python -m chelys "$@"
