#!/usr/bin/env bash
#
# Run the sync from a local scheduler, for when GitHub's own scheduler will not.
# The `schedule` event has never fired on this repo while manual runs succeed,
# which GitHub documents as possible ("some queued jobs may be dropped") and
# does not guarantee. Actions still works on demand; only the clock is missing.
#
#   ./tools/run-sync.sh              # publish
#   ./tools/run-sync.sh --dry-run    # any sync flag is passed through
#
# Install with cron (every 15 minutes, off the busy quarter-hours):
#   crontab -e
#   7,22,37,52 * * * * /home/Mihar/WBD/gqa-gonogo-version-sync/tools/run-sync.sh
#
# or with the systemd user timer next to this script:
#   mkdir -p ~/.config/systemd/user
#   cp tools/gonogo-sync.{service,timer} ~/.config/systemd/user/
#   systemctl --user daemon-reload
#   systemctl --user enable --now gonogo-sync.timer
#   loginctl enable-linger "$USER"   # keep it running when logged out

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${GONOGO_LOG:-$REPO/sync.log}"

cd "$REPO"

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S %z') ==="
    .venv/bin/python -m gonogo.sync --config config/clients.yml "$@"
    status=$?
    echo "--- exit $status ---"
    exit $status
} >> "$LOG" 2>&1
