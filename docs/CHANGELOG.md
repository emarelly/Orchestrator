# Changelog

All notable changes, per version. The version is `orchestrator.__version__` (and `pyproject.toml`).

## Unreleased

### Changed
* Timer events without a sun or moon time now run from the start to the end of the calendar event
  itself (`Timer: relaynum 4`); `end`, `duration` and a clock `start` in the title are ignored.
  Titles with a sun time (`start sunset`, `end sunrise`) work as before. The page writes the event
  end for such timers. FR-CFG-6.
* The calendar is read for the coming seven days (today 00:00 to +7 days) instead of the Sunday to
  Saturday week, so the Saturday-to-Sunday midnight and one-off events for next week are taken.
  FR-CFG-1.

## 0.1.0 - first complete version (not yet run on the real Pi)

Built and tested against fakes only; acceptance on the Pi (PLAN.md section 3, M8) is still to do.

### Added
* Schedules: parser and byte-exact serializer for `Cal.json` / `TimeCal.json`, validation with the
  limit of 5 showers, day numbering 1 = Sunday.
* Google Calendar: reads the week (recurring events expanded), event-title grammar of the old
  scripts, sunrise / sunset times with the daylight-saving offset of the event date, optional
  write-back of page edits to the calendar (off by default), last-change-wins conflicts.
* Boiler Pi: REST client, status poller with down detection, push queue with change detection and
  retry (empty schedules are never sent).
* Manual control: On / Off / Auto and relay runs (relays 2-7), the same code for the web page, MQTT
  and e-mail.
* Web page (Flask + waitress): status, chart, schedules, manual buttons, relays, system tab; JSON
  API with same-origin check, size limit and optional login with failed-login limit.
* E-mail commands: sender validation, authentication result, two-step token confirmation, lockout,
  security alerts with rate limits, SSH reboot of the boiler Pi with host-key check.
* Home Assistant: MQTT with last will, discovery of 28 entities, state, commands.
* Admin notices: boiler Pi down/back, calendar changes and failures, Google authorization,
  disk / memory / temperature / clock / pending-push alerts, hourly heartbeat line.
* Operation: start-up in the order of OPS-2, supervisor with watchdog and `/healthz`, systemd unit,
  `deploy/install.sh` and `deploy/rollback.sh`.
* Tools: `python -m orchestrator.auth`, `python -m orchestrator.ssh_reboot --show-host-key`,
  `tools/check.py`, `tools/ui_smoke.py`, `tools/freeze_tested.py`, `tools/check_pi_install.sh`.

### Changed (2026-10-28)
* Manual control uses the boiler Pi's own endpoints (`manualon/`, `manualoff/`, `auto/`, `relayon/`)
  instead of uploading manual files (OQ-16). The boiler Pi chooses the level (4) and the time (about
  3 h On, about an hour Off); a chosen duration is refused. Relay on and off are `relayon/` and
  `relayoff/`; a duration of 0 or less means without a time limit (OQ-14). Config keys `manual_upload_path` and
  `relay_manual_upload_path` are gone; `manual_on_path`, `manual_off_path` and `manual_auto_path`
  are new.
* The history answer of the boiler Pi is read as text and its month is 1-based; the three tank
  levels use fixed sensor positions; the page redraws the chart every minute.

### Google logins
* The mail and the calendar may use two different logins (`GOOGLE_TOKEN_FILE` and the optional
  `GOOGLE_CALENDAR_TOKEN_FILE`), so the old Gmail token and the old read-only Calendar token can be
  reused; `deploy/install.sh` converts them (`--google-from`, `--no-google-import`). Page edits need
  the full calendar permission.

### Known gaps
* `[calendar]`/`ui`/`both` modes (FR-CFG-3): the page edits the calendar; there is no separate
  local schedule store, so a setup without Google cannot edit schedules.
* `--import-oauth2client` takes one token file; the folder form is done by `deploy/install.sh`.
* Coverage is not measured (needs `pytest-cov`, not yet approved); `pip-audit` is not yet run.
* Nothing has been run on the real Pi, with real Google, MQTT broker or boiler Pi.
