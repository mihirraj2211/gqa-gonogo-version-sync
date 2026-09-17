#!/usr/bin/env bash
#
# The health probe on the same 15-minute beat as the sync, from a scheduler
# that fires. It checks what a green sync run cannot: that the feed still
# carries every platform, and that the page still has every row the config
# names. Payloads are not dumped by default, since the log is kept on disk.
#
#   ./tools/run-probe.sh                # check
#   ./tools/run-probe.sh --show-payload # with the raw API response
#
# Installed by gonogo-probe.timer, which runs seven minutes off the sync so the
# two do not hit the API together. See tools/run-sync.sh for the install.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${GONOGO_PROBE_LOG:-$REPO/probe.log}"

cd "$REPO"

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S %z') ==="
    .venv/bin/python -m gonogo.probe \
        --config config/clients.yml \
        --min-platforms "${MIN_PLATFORMS:-1}" \
        --min-rows "${MIN_ROWS:-1}" \
        "$@"
    status=$?
    echo "--- exit $status ---"
    exit $status
} >> "$LOG" 2>&1
