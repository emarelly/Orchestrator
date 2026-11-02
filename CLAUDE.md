# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

The service is implemented and tested against fakes (milestones M0-M7 of `docs/PLAN.md` are done); what is left is the acceptance on the real Pi (M8, done by the user) and the open decisions in `docs/PLAN.md` section 5. `README.md` explains install and operation, `docs/CHANGELOG.md` lists the versions, `docs/traceability.md` maps every requirement to a test. `docs/Requirements.md` is the source of truth (numbered IDs such as FR-MAN-2, CS-4, OPS-6, OQ-16; open questions are in its section 8). The planned check command is `python -m tools.check` (black, ruff, mypy, pytest, CS-4) and the Pi runs **Python 3.13.5** (32-bit Raspberry Pi OS, Pi 3) and the code targets Python 3.11+ (tested on 3.13, see CS-1), Flask served by waitress, systemd service. Update `Requirements.md` when a decision changes rather than leaving it stale.

- `docs/Requirements.md` - requirements, standards (CS), logging (LOG), service operation (OPS).
- `ui-mock/index.html` - reviewed, approved layout of the new UI (single file, no dependencies, inline SVG). Open it directly in a browser; there is no build step. It is the reference for the real UI (FR-UI-*), including the mobile behaviour.
- `requirements.txt` (runtime, unpinned), `requirements-dev.txt` (dev tools), `tools/check_pi_install.sh` - run `bash tools/check_pi_install.sh` on the Pi to trial-install the runtime libraries on Python 3.13/armv7l; it writes `requirements.lock` (the versions to deploy) and `pi_install_report.txt`.
- `Cal.json`, `TimeCal.json` - real sample schedules; they are the golden files for the byte-for-byte serializer test (FR-PUSH-7).
- `ref files/` - the legacy scripts and page this project replaces. Read-only reference: port behaviour with tests, never import from it.

## Architecture (big picture)

The Orchestrator is a separate LAN service (Pi 3) that replaces the legacy `BoilerControler.py` + `BoilerCalander.py`/`TimerCalander.py`. It does **not** touch files on the boiler Pi; everything goes through the boiler Pi's REST API (`baseRestURL`, port 8000, paths end with `/`). The boiler and timer services on that Pi do the actual heating/relay switching from the files the Orchestrator sends.

Data flow: Google Calendar events (title grammar parsed by `ref files/ReadGmailCalendar.py`) and UI edits -> schedule model -> `Cal.json` / `TimeCal.json` built by the Orchestrator -> multipart file POST (field `file`) to the boiler Pi. Status flows back from `GET status/` (and `history/`). UI, MQTT/Home Assistant and e-mail commands are three front ends over the same manual/status logic. Worker threads (status poll, calendar, mail, MQTT, push queue) write to a locked cache; HTTP handlers only read it.

Manual control goes through the boiler Pi's own REST endpoints (OQ-16, resolved 2026-10-28; `ref files/ManagerRestApi.py`): On -> `POST manualon/`, Off -> `POST manualoff/`, Auto -> `POST auto/`, relay run -> `POST relayon/` with the JSON line `{"swith": n, "duration": s}`. The Orchestrator uploads no manual files; the boiler Pi builds them. The boiler Pi deletes `manual.json` at its expiry time. The upload endpoint URLs for these manual files are not defined yet (OQ-16, deferred); keep them as config keys.

## Constraints that are easy to get wrong

- **The boiler/timer services parse schedule files by string-splitting, not JSON.** Output must be compact, no spaces/newline/BOM, objects separated by `},{`, fixed key order, `HH:MM:SS`, sorted, no duplicates. An empty schedule is never pushed (only an empty manual file). The legacy parser reads `[]` as one default task (day 0), see OQ-17.
- **Day of week: 1 = Sunday ... 7 = Saturday.** Python's `weekday()` is Monday = 0; convert in one place. Week wraps 7 -> 1.
- **Showers are the unit of Target, Min and capacity.** Hard limit 5 for every input channel (calendar, UI, MQTT, e-mail, manual). `Min` is the level kept at all times during a slot's period, `Target` is what the heater heats to.
- **Manual control is decided by the boiler Pi:** On = `manualon/` (Target and Min 4, about 3 h), Off = `manualoff/` (0/0, about an hour), Auto = `auto/`. Never build a manual file, level or expiry time here. Relay on / off are `relayon/` / `relayoff/` with `{"swith": n, "duration": s}`; a duration of 0 or less means without a time limit.
- **`/status` `Status` is `Auto`, `Manual` or `Off`.** Both shower values are already in showers. Upper/Mid/Lower temperature uses fixed sensor positions (Upper = `CurrentTemp[2]`, Mid = `[0]`, Lower = `[1]`), never sorted by value. `temp.txt.current` is not accessible and not used.
- **Relays 0 and 1 belong to the boiler service**; manual relay commands are 2-7 only. Relay names (`Name2RelayNum`) are matched case-insensitively and names 6/7 are currently duplicates.
- Calendar event titles use the legacy grammar (`Boiler: target=3.5; min=2`, `Timer: relaynum 4 start sunset +30 end 23:00 | duration 3600`). Astro times must be resolved with the event date's DST offset (legacy used the current offset and a fixed `+02:00`).
- **E-mail commands**: sender must be in `UserEmailList` (alert to `SecurityAlertEmail` otherwise), state-changing commands need the two-factor token flow (FR-LEG-3b, FR-LEG-4). Secrets come from environment variables, never from files in the repo.
- Legacy defects not to copy are listed in Requirements section 3.7 (e.g. `Post()` sending a second request after a file upload, `Status == "on"` checks, import-time side effects).
## Safety rules (this service controls a heater)

- **Never contact the real boiler Pi from tests or local dev.** Tests mock the REST API. A `DRY_RUN` config flag builds and logs the files without POSTing them.
- **Fail safe.** If a status poll or push fails, log it and retry. Never guess the heater state and never push a partial or empty schedule.
- **Golden files are read-only.** Never edit or regenerate `Cal.json` / `TimeCal.json` to make a test pass. If the serializer output differs, fix the serializer.
- **Open questions: ask, don't invent.** If a task touches an OQ item in Requirements section 8 (e.g. OQ-17 empty schedule), stop and ask. Keep undecided values as config keys.
- **No new dependencies without asking.** The target is a Pi 3 with limited RAM and SD-card writes. Prefer the standard library.
- **Never silence a check.** No `# type: ignore`, `# noqa`, skipped or deleted tests, or loosened lint/mypy config to get `python -m tools.check` green. Fix the cause or ask.
- **Secrets only from environment variables.** Never in the repo, logs or test fixtures.

## Python and commands

- Runtime: Python 3.13.5 on the Pi (Raspberry Pi OS Trixie, 32-bit). Minimum supported version is 3.11, tested on 3.13: use modern Python (`X | None` unions, built-in generics like `list[str]`, `match`, `tomllib`, `StrEnum`, `dataclass(slots=True)`), but nothing newer than 3.13 and not the 3.12-only `type` statement or PEP 695 generics (the minimum is 3.11).
- Environment: virtual environment `.venv` (Python 3.13) in this folder, created with `py -3.13 -m venv .venv` and `.venv\Scripts\python -m pip install -r requirements-dev.txt` (Pi: `.venv/bin/python`). Call tools through it, e.g. `.venv\Scripts\python -m tools.check`.
- Full check: `python -m tools.check` (black, ruff, mypy, pytest, CS-4). Fix formatting with `python -m black orchestrator tools tests`.
- Single test: `pytest tests/<file>.py::<test> -x`
- Run locally: `<command>` with `DRY_RUN=1`.
- Tests live in `tests/`. Freeze time in tests; never depend on the real clock. Changes touching day-of-week conversion, DST/astro times or the serializer must come with tests.

## Definition of done

1. `python -m tools.check` passes with no new warnings.
2. Tests added or updated for the change.
3. `docs/Requirements.md` updated if a decision changed.
4. The requirement IDs (FR-*, CS-*, OPS-*) are cited in the commit message.
5. A short summary of what changed and what was not verified.

## Review checklist (for the Opus review pass)

- Output stays compatible with the string-splitting parser: compact, fixed key order, `},{` separators, `HH:MM:SS`, sorted, no duplicates, never an empty schedule.
- Day of week is converted in exactly one place (1 = Sunday ... 7 = Saturday, wrap 7 -> 1).
- The 5-shower cap is enforced on every input channel (calendar, UI, MQTT, e-mail, manual).
- Manual commands are single POSTs to the boiler Pi's endpoints; no manual file, level or expiry is built here; a chosen duration is refused.
- Astro times use the event date's DST offset, not the current offset.
- Relays 0 and 1 are never commanded manually. Only 2-7 are allowed.
- Shared cache access is locked. HTTP handlers only read it.
- E-mail commands enforce the `UserEmailList` and two-factor checks.
- No legacy defect from Requirements section 3.7 was copied.
- No test mutates golden files or contacts real hosts.

## Keeping this file current

When the build, lint and test setup lands, update the "Project state" section at the top and remove the "not implemented yet" wording.