#!/usr/bin/env sh
# Test a candidate on the phone before copying it over the installed code.
# Usage, from the chelys folder:  sh scripts/deploy.sh
set -eu
host="${CHELYS_HOST:-galaxy-a10e}"
cd "$(dirname "$0")/.."
tar --exclude=__pycache__ -cf - chelys tests scripts chelys.sh |
  ssh -o BatchMode=yes "$host" '
    set -eu
    stage=$(mktemp -d "$HOME/.chelys-deploy.XXXXXX")
    trap '\''rm -rf "$stage"'\'' EXIT HUP INT TERM
    tar -xf - -C "$stage"
    chmod 755 "$stage/chelys.sh"
    cd "$stage"
    if python -m unittest discover -s tests -t . >test.log 2>&1; then
      status=0
    else
      status=$?
    fi
    tail -3 test.log
    if [ "$status" -ne 0 ]; then exit "$status"; fi
    mkdir -p "$HOME/chelys"
    tar --exclude=__pycache__ -cf release.tar chelys tests scripts chelys.sh
    tar -xf release.tar -C "$HOME/chelys"
  '
