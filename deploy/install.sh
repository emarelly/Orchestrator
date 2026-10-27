#!/usr/bin/env bash
# Install or update the Orchestrator on the Pi (OPS-1, OPS-4, OPS-5, CS-16).
#
#   sudo bash deploy/install.sh [--no-start] [--volatile-journal] [--google-from DIR] [--no-google-import]
#
# The program lives in /opt/orchestrator/current (with its own virtual environment). An update stops
# the service, keeps the old program as /opt/orchestrator/previous, installs the new one in
# "current" and starts it. If that fails, or the service is not active within a minute, the old
# program is put back by itself. Because of the stop the service is away for the time of the
# installation (a few minutes on a Pi 3); the boiler and timer keep working on their last files.
# deploy/rollback.sh swaps current and previous again later.
#
# Never overwritten by an update: /etc/orchestrator/config.toml, /etc/orchestrator/orchestrator.env
# and everything in /var/lib/orchestrator (schedules, tokens, location cache).
#
# The Google logins of the old scripts (client_secret.json and .credentials/*-python-quickstart.json
# in the source folder, or in DIR) are converted and installed on the first run (see below).
#
# Options for testing without a Pi:
#   --destdir DIR   put everything below DIR; no user, no systemctl, no chown
#   --no-venv       do not create the virtual environment or install packages

set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESTDIR=""
START=1
VENV=1
JOURNAL=0
GOOGLE_IMPORT=1
GOOGLE_FROM="$SOURCE"
SERVICE_USER="orchestrator"

usage() { sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
say() { echo "install: $*"; }
die() { echo "install: ERROR: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --destdir) DESTDIR="${2:?--destdir needs a folder}"; shift 2 ;;
        --no-start) START=0; shift ;;
        --no-venv) VENV=0; shift ;;
        --volatile-journal) JOURNAL=1; shift ;;
        --google-from) GOOGLE_FROM="${2:?--google-from needs a folder}"; shift 2 ;;
        --no-google-import) GOOGLE_IMPORT=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done

PREFIX="$DESTDIR/opt/orchestrator"
ETC="$DESTDIR/etc/orchestrator"
VAR="$DESTDIR/var/lib/orchestrator"
LOGDIR="$DESTDIR/var/log/orchestrator"
UNIT_DIR="$DESTDIR/etc/systemd/system"
STAGING=0
[ -n "$DESTDIR" ] && STAGING=1

# commands that change the real system are only run for a real install
sys() { if [ "$STAGING" = 1 ]; then say "(staging) skipped: $*"; else "$@"; fi; }
active() { [ "$STAGING" = 0 ] && systemctl is-active --quiet orchestrator.service; }

if [ "$STAGING" = 0 ] && [ "$(id -u)" != 0 ]; then die "run this script as root (sudo)"; fi
[ -f "$SOURCE/orchestrator/__init__.py" ] || die "no orchestrator package in $SOURCE"
for file in orchestrator.service orchestrator.env.example config.pi.toml; do
    [ -f "$SOURCE/deploy/$file" ] || die "deploy/$file is missing"
done
if [ "$VENV" = 1 ]; then
    command -v python3 >/dev/null || die "python3 is not installed"
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
        || die "Python 3.11 or newer is needed (found $(python3 --version 2>&1))"
fi

# --- user and folders -----------------------------------------------------------------------------
if [ "$STAGING" = 0 ] && ! id "$SERVICE_USER" >/dev/null 2>&1; then
    say "creating the system user $SERVICE_USER"
    useradd --system --home-dir /var/lib/orchestrator --shell /usr/sbin/nologin "$SERVICE_USER"
fi
mkdir -p "$PREFIX" "$ETC" "$VAR/google" "$VAR/ssh" "$LOGDIR" "$UNIT_DIR"
sys chown root:"$SERVICE_USER" /etc/orchestrator
sys chmod 750 /etc/orchestrator
sys chown -R "$SERVICE_USER":"$SERVICE_USER" /var/lib/orchestrator /var/log/orchestrator
sys chmod 750 /var/lib/orchestrator /var/log/orchestrator
sys chmod 700 /var/lib/orchestrator/google /var/lib/orchestrator/ssh

# --- configuration (created once, never overwritten) -------------------------------------------------
if [ ! -f "$ETC/config.toml" ]; then
    cp "$SOURCE/deploy/config.pi.toml" "$ETC/config.toml"
    sys chown root:"$SERVICE_USER" /etc/orchestrator/config.toml
    sys chmod 640 /etc/orchestrator/config.toml
    say "created $ETC/config.toml - edit it (e-mail addresses, boiler address, calendar)"
fi
if [ ! -f "$ETC/orchestrator.env" ]; then
    cp "$SOURCE/deploy/orchestrator.env.example" "$ETC/orchestrator.env"
    sys chown root:root /etc/orchestrator/orchestrator.env
    sys chmod 600 /etc/orchestrator/orchestrator.env
    say "created $ETC/orchestrator.env - put the secrets in it (mode 600, root only)"
fi
cp "$SOURCE/deploy/orchestrator.service" "$UNIT_DIR/orchestrator.service"
chmod 644 "$UNIT_DIR/orchestrator.service"

if [ "$JOURNAL" = 1 ]; then
    mkdir -p "$DESTDIR/etc/systemd/journald.conf.d"
    printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=20M\n' \
        > "$DESTDIR/etc/systemd/journald.conf.d/orchestrator.conf"
    sys systemctl restart systemd-journald
    say "the journal is kept in memory only (spares the SD card; the log files stay on disk)"
fi

# --- put the old program aside and install the new one in "current" ------------------------------------
WAS_ACTIVE=0
if active; then WAS_ACTIVE=1; fi
if [ "$WAS_ACTIVE" = 1 ]; then say "stopping the service for the update"; systemctl stop orchestrator.service; fi

HAD_OLD=0
if [ -d "$PREFIX/current" ]; then
    rm -rf "$PREFIX/previous"
    mv "$PREFIX/current" "$PREFIX/previous"
    HAD_OLD=1
fi

put_old_back() {
    rm -rf "$PREFIX/current"
    if [ "$HAD_OLD" = 1 ]; then mv "$PREFIX/previous" "$PREFIX/current"; fi
    if [ "$WAS_ACTIVE" = 1 ]; then systemctl start orchestrator.service || true; fi
}
trap 'say "the installation failed; putting the old program back"; put_old_back' ERR

mkdir -p "$PREFIX/current"
say "copying the program into $PREFIX/current"
cp -r "$SOURCE/orchestrator" "$PREFIX/current/orchestrator"
find "$PREFIX/current/orchestrator" -name '__pycache__' -prune -exec rm -rf {} +
for file in requirements.txt requirements.lock config.example.toml README.md docs/INSTALL.md docs/CHANGELOG.md; do
    if [ -f "$SOURCE/$file" ]; then
        mkdir -p "$(dirname "$PREFIX/current/$file")"
        cp "$SOURCE/$file" "$PREFIX/current/$file"
    fi
done
date '+installed %Y-%m-%d %H:%M:%S %z' > "$PREFIX/current/RELEASE"

if [ "$VENV" = 1 ]; then
    REQ="$PREFIX/current/requirements.lock"
    if [ ! -f "$REQ" ]; then
        REQ="$PREFIX/current/requirements.txt"
        say "WARNING: no requirements.lock, installing the unpinned requirements.txt"
    fi
    [ -f "$REQ" ] || die "neither requirements.lock nor requirements.txt found"
    say "creating the virtual environment and installing $(basename "$REQ") (slow on a Pi 3)"
    python3 -m venv "$PREFIX/current/venv"
    "$PREFIX/current/venv/bin/python" -m pip install --quiet --no-input --upgrade pip
    "$PREFIX/current/venv/bin/python" -m pip install --quiet --no-input -r "$REQ"
else
    say "(--no-venv) virtual environment skipped"
fi
sys chown -R root:root /opt/orchestrator
sys chmod -R go-w /opt/orchestrator
trap - ERR

# --- reuse the logins of the old scripts (never overwritten) ------------------------------------------
# From the folder given with --google-from (default: the source folder) the old files are used:
#   client_secret.json                      -> /var/lib/orchestrator/google/client_secret.json
#   .credentials/Gmail-python-quickstart.json    -> .../token.json           (for the mail)
#   .credentials/Calendar-python-quickstart.json -> .../calendar_token.json  (for the calendar)
# They are converted to the new format; the old files are left where they are (delete them when the
# service works). Nothing that already exists in the target folder is touched.
import_google() {
    local from="$1" google="$VAR/google" python
    if [ "$VENV" = 1 ]; then python="$PREFIX/current/venv/bin/python"; else python="${IMPORT_PYTHON:-python3}"; fi
    if [ -f "$from/client_secret.json" ] && [ ! -f "$google/client_secret.json" ]; then
        (umask 077; cp "$from/client_secret.json" "$google/client_secret.json")
        sys chown "$SERVICE_USER":"$SERVICE_USER" /var/lib/orchestrator/google/client_secret.json
        say "copied client_secret.json"
    fi
    convert_old_token "$from/.credentials/Gmail-python-quickstart.json" "$google/token.json" mail "$python"
    convert_old_token "$from/.credentials/Calendar-python-quickstart.json" "$google/calendar_token.json" calendar "$python"
}
convert_old_token() {
    local source="$1" target="$2" purpose="$3" python="$4" report
    if [ ! -f "$source" ]; then say "no old $(basename "$source") found, skipped"; return 0; fi
    if [ -f "$target" ]; then say "$(basename "$target") exists already, not touched"; return 0; fi
    if report="$( (umask 077; PYTHONPATH="$PREFIX/current" "$python" -m orchestrator.auth \
            --import-oauth2client "$source" --token "$target" --for "$purpose") 2>&1)"; then
        sys chown "$SERVICE_USER":"$SERVICE_USER" "/var/lib/orchestrator/google/$(basename "$target")"
        say "converted $(basename "$source") for the $purpose: $report"
    else
        rm -f "$target"
        say "WARNING: $(basename "$source") could not be used ($report); authorize again with"
        say "         python -m orchestrator.auth"
    fi
}
if [ "$GOOGLE_IMPORT" = 1 ]; then import_google "$GOOGLE_FROM"; fi

# --- start it, and go back if it does not come up -----------------------------------------------------
sys systemctl daemon-reload
sys systemctl enable orchestrator.service
if [ "$START" = 1 ]; then
    sys systemctl start orchestrator.service
    if [ "$STAGING" = 0 ]; then
        for _ in $(seq 1 30); do
            if active; then break; fi
            sleep 2
        done
        if ! active; then
            journalctl -u orchestrator.service -n 30 --no-pager || true
            systemctl stop orchestrator.service || true
            rm -rf "$PREFIX/failed"
            mv "$PREFIX/current" "$PREFIX/failed"
            if [ "$HAD_OLD" = 1 ]; then
                mv "$PREFIX/previous" "$PREFIX/current"
                systemctl start orchestrator.service || true
                die "the new program did not start (kept in $PREFIX/failed); the old one is back"
            fi
            die "the service did not become active (the program is kept in $PREFIX/failed)"
        fi
        say "the service is active"
    fi
else
    say "(--no-start) the program is installed and enabled but not started"
fi

say "done. Next: edit $ETC/config.toml and $ETC/orchestrator.env, authorize Google"
say "      (python -m orchestrator.auth), then: systemctl status orchestrator"
