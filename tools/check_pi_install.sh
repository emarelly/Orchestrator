#!/usr/bin/env bash
# Trial install of requirements.txt on the target Pi (Raspberry Pi OS Trixie, Python 3.13, 32-bit armv7l).
#
# What it does (nothing outside its own folder is changed except an optional "apt install" you do yourself):
#   1. checks OS, CPU, Python, free memory/disk and the system packages the build needs
#   2. creates a throw-away virtual environment and upgrades pip
#   3. installs requirements.txt twice: first wheels only (fast, no compiling), then, only if that failed,
#      normally (may compile; slow on a Pi 3) and reports which packages had to be built from source
#   4. imports every library and reports the memory used after importing them all
#   5. writes requirements.lock (exact versions that worked) and a report file, then deletes the virtual env
#
# Usage:   cd <project folder> && bash tools/check_pi_install.sh
# Output:  requirements.lock, pi_install_report.txt   (send me the report)

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$(mktemp -d /tmp/orch-install-check.XXXXXX)"
VENV="$WORK_DIR/venv"
REPORT="$PROJECT_DIR/pi_install_report.txt"
LOCK="$PROJECT_DIR/requirements.lock"
REQ="$PROJECT_DIR/requirements.txt"

: > "$REPORT"
say() { echo "$*" | tee -a "$REPORT"; }
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

if [ ! -f "$REQ" ]; then echo "requirements.txt not found in $PROJECT_DIR"; exit 2; fi

say "=== Orchestrator install check  $(date '+%Y-%m-%d %H:%M:%S %z') ==="

say ""
say "--- system"
. /etc/os-release 2>/dev/null; say "OS:        ${PRETTY_NAME:-unknown}"
say "Kernel:    $(uname -srm)"
PYBIN="${PYTHON:-python3}"
say "Python:    $($PYBIN --version 2>&1)  ($(command -v $PYBIN))"
say "systemd:   $(systemctl --version 2>/dev/null | head -n1)"
say "Memory:    $(free -m | awk '/^Mem:/ {print $2" MB total, "$7" MB available"}')"
say "Swap:      $(free -m | awk '/^Swap:/ {print $2" MB"}')"
say "Disk free: $(df -h "$PROJECT_DIR" | awk 'NR==2 {print $4" free on "$1}')"
TEMP="$(vcgencmd measure_temp 2>/dev/null || true)"; [ -n "$TEMP" ] && say "CPU temp:  $TEMP"
if [ "$(uname -m)" != "armv7l" ]; then say "NOTE: this is not armv7l (32-bit Pi 3), so the result does not describe the target Pi."; fi

say ""
say "--- system packages needed for the build / venv"
MISSING=""
for pkg in python3-venv python3-dev libffi-dev libssl-dev; do
  if dpkg -s "$pkg" >/dev/null 2>&1; then say "  ok       $pkg"; else say "  MISSING  $pkg"; MISSING="$MISSING $pkg"; fi
done
if [ -n "$MISSING" ]; then
  say "Install first:  sudo apt update && sudo apt install -y$MISSING"
  say "(If apt cannot find a package, run sudo apt update first.)"
  if ! dpkg -s python3-venv >/dev/null 2>&1; then say "Cannot continue without python3-venv."; exit 3; fi
fi

say ""
say "--- virtual environment"
"$PYBIN" -m venv "$VENV" >>"$REPORT" 2>&1 || { say "venv creation failed"; exit 4; }
PIP="$VENV/bin/pip"; PY="$VENV/bin/python"
"$PIP" install --quiet --upgrade pip wheel setuptools >>"$REPORT" 2>&1
say "pip:       $($PIP --version)"

say ""
say "--- install, step 1: wheels only (no compiling)"
START=$(date +%s)
"$PIP" install --only-binary=:all: -r "$REQ" >"$WORK_DIR/step1.log" 2>&1
RC1=$?
say "result: exit code $RC1, $(( $(date +%s) - START )) s"
INSTALLED_FROM="wheels"
if [ $RC1 -ne 0 ]; then
  say "Step 1 failed. Last lines:"; tail -n 15 "$WORK_DIR/step1.log" | sed 's/^/    /' | tee -a "$REPORT"
  say ""
  say "--- install, step 2: normal install (may compile; can take several minutes on a Pi 3)"
  START=$(date +%s)
  "$PIP" install -r "$REQ" >"$WORK_DIR/step2.log" 2>&1
  RC2=$?
  say "result: exit code $RC2, $(( $(date +%s) - START )) s"
  INSTALLED_FROM="wheels + source builds"
  BUILT="$(grep -E 'Building wheel for|Running setup.py install for' "$WORK_DIR/step2.log" | sort -u)"
  if [ -n "$BUILT" ]; then say "Built from source:"; echo "$BUILT" | sed 's/^/    /' | tee -a "$REPORT"; fi
  if [ $RC2 -ne 0 ]; then
    say "Step 2 failed. Last lines:"; tail -n 25 "$WORK_DIR/step2.log" | sed 's/^/    /' | tee -a "$REPORT"
    say "INSTALL FAILED - send pi_install_report.txt"; exit 5
  fi
fi

say ""
say "--- installed versions (also written to requirements.lock)"
"$PIP" freeze | sort -f > "$LOCK"
"$PIP" freeze | sort -f | sed 's/^/    /' | tee -a "$REPORT"
say "Installed from: $INSTALLED_FROM"

say ""
say "--- import test and memory"
"$PY" - <<'PYEOF' 2>&1 | tee -a "$REPORT"
import importlib, resource, sys, time

def rss_mb():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    return -1.0

modules = [
    "flask", "waitress", "requests", "paho.mqtt.client",
    "google.oauth2.credentials", "google_auth_oauthlib.flow", "googleapiclient.discovery",
    "astral", "astral.sun", "astral.moon", "tzdata", "paramiko", "sdnotify", "zoneinfo",
]

print("RSS at start: %.1f MB" % rss_mb())
failed = []
for name in modules:
    t0 = time.time()
    before = rss_mb()
    try:
        importlib.import_module(name)
        print("  ok     %-28s +%.1f MB  %.2f s" % (name, rss_mb() - before, time.time() - t0))
    except Exception as exc:                      # report every failure, do not stop at the first
        failed.append(name)
        print("  FAILED %-28s %s: %s" % (name, type(exc).__name__, exc))

# a time-zone and an astro calculation, the two things most likely to break
try:
    from zoneinfo import ZoneInfo
    import datetime
    from astral import LocationInfo
    from astral.sun import sun
    tz = ZoneInfo("Asia/Jerusalem")
    loc = LocationInfo(latitude=32.08, longitude=34.81)
    s = sun(loc.observer, date=datetime.date(2026, 7, 1), tzinfo=tz)
    print("  ok     astral sunset 2026-07-01 Asia/Jerusalem: %s" % s["sunset"].isoformat())
except Exception as exc:
    failed.append("astral+zoneinfo")
    print("  FAILED astral + time zone test: %s: %s" % (type(exc).__name__, exc))

print("RSS after importing everything: %.1f MB (budget for the whole service: 120 MB)" % rss_mb())
print("peak RSS: %.1f MB" % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0))
print("IMPORT RESULT: " + ("all ok" if not failed else "FAILED: " + ", ".join(failed)))
sys.exit(1 if failed else 0)
PYEOF
IMPORT_RC=${PIPESTATUS[0]}

say ""
say "=== summary ==="
say "install:  ok ($INSTALLED_FROM)"
say "imports:  $([ "$IMPORT_RC" -eq 0 ] && echo ok || echo FAILED)"
say "lock file: $LOCK"
say "report:    $REPORT"
exit "$IMPORT_RC"
