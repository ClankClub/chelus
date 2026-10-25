#!/usr/bin/env sh
# Copy the agent to the phone (SSH host alias "galaxy-a10e" from ~/.ssh/config) and run its tests there.
# Usage, from the chelus folder:  sh scripts/deploy.sh
set -eu
host="${CHELUS_HOST:-galaxy-a10e}"
cd "$(dirname "$0")/.."
tar --exclude=__pycache__ -cf - chelus tests chelus.sh |
  ssh -o BatchMode=yes "$host" 'mkdir -p ~/chelus && tar -xf - -C ~/chelus && chmod 755 ~/chelus/chelus.sh && cd ~/chelus && python -m unittest discover -s tests -t . 2>&1 | tail -3'
