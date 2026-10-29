#!/usr/bin/env bash
# Install (or update) Delphi as a systemd user service that runs from this checkout, starts on boot and restarts if
# it crashes. Safe to re-run after pulling changes. Usage: ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR=$PWD

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
.venv/bin/playwright install chromium >/dev/null || .venv/bin/playwright install --with-deps chromium

if ! .venv/bin/python bot.py --check; then
    echo "Fix the above, then run ./deploy.sh again. (The service isn't installed until the check passes.)"
    exit 1
fi

mkdir -p ~/.config/systemd/user
.venv/bin/python -c 'import sys; print(open("delphi.service").read().replace("%h/Projects/Delphi", sys.argv[1]), end="")' \
    "$DIR" > ~/.config/systemd/user/delphi.service
systemctl --user daemon-reload
systemctl --user enable delphi >/dev/null
systemctl --user restart delphi
loginctl enable-linger "$USER" 2>/dev/null || echo "Couldn't enable lingering: run 'sudo loginctl enable-linger $USER' so it keeps running after you log out."
sleep 3
systemctl --user --no-pager status delphi | head -5
echo "Running. Logs: journalctl --user -u delphi -f"
