#!/usr/bin/env bash
# Go back to the previous program version of the Orchestrator (OPS-4).
#
#   sudo bash deploy/rollback.sh [--destdir DIR]
#
# Swaps /opt/orchestrator/current and /opt/orchestrator/previous and restarts the service. Run it
# again to swap back. Configuration, secrets and data are shared and not touched.

set -euo pipefail

DESTDIR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --destdir) DESTDIR="${2:?--destdir needs a folder}"; shift 2 ;;
        -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "rollback: unknown option: $1" >&2; exit 1 ;;
    esac
done

PREFIX="$DESTDIR/opt/orchestrator"
[ -d "$PREFIX/current" ] || { echo "rollback: no current program" >&2; exit 1; }
[ -d "$PREFIX/previous" ] || { echo "rollback: there is no previous program" >&2; exit 1; }

if [ -z "$DESTDIR" ]; then
    [ "$(id -u)" = 0 ] || { echo "rollback: run this script as root (sudo)" >&2; exit 1; }
    systemctl stop orchestrator.service || true
fi
mv "$PREFIX/current" "$PREFIX/swap"
mv "$PREFIX/previous" "$PREFIX/current"
mv "$PREFIX/swap" "$PREFIX/previous"
echo "rollback: now running: $(cat "$PREFIX/current/RELEASE" 2>/dev/null || echo unknown)"
echo "rollback: kept as previous: $(cat "$PREFIX/previous/RELEASE" 2>/dev/null || echo unknown)"
if [ -z "$DESTDIR" ]; then
    systemctl start orchestrator.service
    if systemctl is-active --quiet orchestrator.service; then
        echo "rollback: the service is active"
    fi
fi
