#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot script: start the Telegram bridge after the phone boots.
# Install: cp scripts/termux-boot-chelus.sh ~/.termux/boot/start-chelus && chmod 700 ~/.termux/boot/start-chelus
# The bridge retries Telegram on its own, so it can start before the network is up.
termux-wake-lock
pgrep -f "chelus telegram" >/dev/null && exit 0
log="$HOME/.local/share/chelus/telegram.log"
mkdir -p "$(dirname "$log")"
cd "$HOME/chelus" || exit 1
nohup setsid ./chelus.sh telegram >> "$log" 2>&1 < /dev/null &
