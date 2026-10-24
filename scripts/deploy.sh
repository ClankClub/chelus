#!/usr/bin/env sh
# Copy the agent to the phone (SSH host alias "galaxy-a10e" from ~/.ssh/config) and run its tests there.
# Usage, from the chelys folder:  sh scripts/deploy.sh
set -eu
host="${CHELYS_HOST:-galaxy-a10e}"
cd "$(dirname "$0")/.."
tar --exclude=__pycache__ -cf - chelys tests chelys.sh |
  ssh -o BatchMode=yes "$host" 'mkdir -p ~/chelys && tar -xf - -C ~/chelys && chmod 755 ~/chelys/chelys.sh && cd ~/chelys && python -m unittest discover -s tests -t . 2>&1 | tail -3'
