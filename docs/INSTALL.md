# Installing the Orchestrator on the Raspberry Pi

Step by step, in order. For what the program does and for troubleshooting see `README.md` in the project folder.

**You need:** a Raspberry Pi 3 with Raspberry Pi OS (Trixie) and Python 3.11 or newer, on the same
network as the boiler Pi, with a working clock (NTP). Steps 1 to 6 are done on the Pi.

## 0. Before you start (on the PC)

* The project folder must contain `client_secret.json` and the folder `.credentials/` with
  `Gmail-python-quickstart.json` and `Calendar-python-quickstart.json` (the logins of the old
  scripts). The install converts them, so there is no new Google sign-in.
* Keep these files out of any commit or shared copy: they are secrets (`.gitignore` excludes them).
* Only if you want the page to edit the calendar later: the Google Cloud project needs the Calendar
  API enabled and the consent screen set to **In production**.

## 1. Copy the project to the Pi

Copy only the files the Pi needs, not the whole project (no tests, mock, reference files, caches or
virtual environment). On the PC, in the project folder (PowerShell or any shell with `tar`):

```
tar -czf orchestrator-pi.tgz --exclude=__pycache__ orchestrator deploy tools/__init__.py tools/check_pi_install.sh tools/perf_check.py requirements.txt config.example.toml README.md docs/INSTALL.md docs/CHANGELOG.md client_secret.json .credentials/Gmail-python-quickstart.json .credentials/Calendar-python-quickstart.json
scp orchestrator-pi.tgz pi@<pi>:~
ssh pi@<pi>
mkdir orchestrator-src && tar -xzf orchestrator-pi.tgz -C orchestrator-src && cd orchestrator-src
```

What is in it and why:

| Files | Needed for |
|---|---|
| `orchestrator/` | the program (including the web page `orchestrator/ui/index.html`) |
| `deploy/` | the install and rollback scripts, the systemd unit, the starting configuration |
| `requirements.txt`, `tools/check_pi_install.sh` | step 2 (library check, writes `requirements.lock`) |
| `config.example.toml` | the list of every setting (copied next to the program) |
| `client_secret.json`, the two `*-python-quickstart.json` files | the conversion of your old Google logins |
| `tools/perf_check.py`, `tools/__init__.py` | the one-hour measurement in step 7 (optional) |
| `README.md`, `docs/INSTALL.md`, `docs/CHANGELOG.md` | documentation (optional; the install copies them next to the program) |

Not needed on the Pi: `tests/`, `ui-mock/`, `ref files/`, the rest of `docs/`, `Cal.json` and
`TimeCal.json` (test samples), `CLAUDE.md`, `pyproject.toml`, `requirements-dev.txt`, `.venv`,
and the old `.NET` token folders in `.credentials`. The archive holds secrets: delete it from the Pi
and the PC when you are done.

## 2. Check that the libraries install on this Pi (once)

```
bash tools/check_pi_install.sh
```

It takes a while on a Pi 3. It installs the libraries in a throw-away environment, tests them, and
writes `requirements.lock` (the exact versions that will be installed) and `pi_install_report.txt`.
If it reports a library that cannot be installed, stop and send the report.

## 3. Install

```
sudo bash deploy/install.sh
```

This:

* creates the user `orchestrator` and the folders `/opt/orchestrator` (program),
  `/etc/orchestrator` (settings and secrets), `/var/lib/orchestrator` (data) and
  `/var/log/orchestrator` (logs);
* builds a virtual environment from `requirements.lock`;
* converts your old Google logins into `/var/lib/orchestrator/google/` (`token.json` for the mail,
  `calendar_token.json` for the calendar; existing files are never overwritten);
* creates `/etc/orchestrator/config.toml` and `/etc/orchestrator/orchestrator.env` the first time;
* installs and starts the systemd service `orchestrator`.

Options: `--no-start`, `--volatile-journal` (keep the system journal in memory to spare the SD card),
`--google-from DIR` (take the old Google files from another folder), `--no-google-import`.

## 4. Configure

Edit the settings, then restart:

```
sudo nano /etc/orchestrator/config.toml
sudo nano /etc/orchestrator/orchestrator.env
sudo systemctl restart orchestrator
```

In `config.toml` set at least:

| Setting | What |
|---|---|
| `[boiler] base_url` | the boiler Pi, for example `http://MARELLYPI3:8000/` |
| `[mail] user_email_list`, `admin_email`, `security_alert_email` | who may send commands, who gets notices and security alerts |
| `[location] latitude`, `longitude` | needed for sunrise / sunset events |
| `[mqtt] enabled`, `host`, `username` | only if you use Home Assistant |

In `orchestrator.env` (secrets, readable by root only) set what you use:

| Variable | What |
|---|---|
| `BOILER_SSH_USER` and `BOILER_SSH_KEY_FILE` (or `BOILER_SSH_PASSWORD`) | the `reset` e-mail command |
| `MQTT_PASSWORD` | the MQTT login |
| `UI_PASSWORD` | the web page login (when `[web] login_required = true`) |

Leave the Google lines as they are. To try everything without sending anything to the boiler Pi,
add `DRY_RUN=1` to this file.

## 5. Check that it works

```
systemctl status orchestrator
curl http://localhost:8080/healthz
journalctl -u orchestrator -n 50
```

Then open `http://<pi>:8080/` in a browser. You should see the boiler status. The chip at the top
should say "Calendar read ... ago". If it says **Google authorization needed**, check the logins:

```
cd /opt/orchestrator/current
sudo -u orchestrator venv/bin/python -m orchestrator.auth --token /var/lib/orchestrator/google/token.json --check --for mail
sudo -u orchestrator venv/bin/python -m orchestrator.auth --token /var/lib/orchestrator/google/calendar_token.json --check --for calendar
```

If Google refuses either one, make a new login (see README, "Google authorization").

### If the service does not start

Where the logs are:

| Where | How to read it |
|---|---|
| systemd and the journal (everything the program prints, including start-up errors) | `sudo journalctl -u orchestrator -b -n 100 --no-pager` and live: `sudo journalctl -u orchestrator -f` |
| state and exit code of the last start | `sudo systemctl status orchestrator -l --no-pager` |
| log file of the program (not written if it failed before logging started) | `sudo tail -n 100 /var/log/orchestrator/orchestrator.log` |
| every action with who did it (audit) | `sudo tail -n 50 /var/log/orchestrator/audit.log` |

The journal is lost at a reboot if you used `--volatile-journal`; the log files stay.

Typical reasons, by what the log or `status` shows:

| Shows | Cause and fix |
|---|---|
| `configuration error: [section] key: ...` (exit status 2) | a mistake in `/etc/orchestrator/config.toml`; the message names the key. Fix it and restart. |
| `status=203/EXEC` or `No such file` for `venv/bin/python` | the virtual environment was not built; run `sudo bash deploy/install.sh` again and read its output. |
| `status=200/CHDIR` | `/opt/orchestrator/current` is missing; run the install again. |
| `status=226/NAMESPACE` | `/var/lib/orchestrator` or `/var/log/orchestrator` is missing; run the install again. |
| `Failed to load environment files` | `/etc/orchestrator/orchestrator.env` is missing; the install creates it, or copy `deploy/orchestrator.env.example` there. |
| `event=fatal` and `Address already in use` | something else uses port 8080; change `[web] port`. |
| `Permission denied` on a file in `/var/lib/orchestrator` | `sudo chown -R orchestrator:orchestrator /var/lib/orchestrator /var/log/orchestrator` |
| it starts, then is killed after about a minute (`watchdog`) | a critical part is stuck; the journal shows `watchdog_ping_withheld` and which component. |
| `start-limit-hit` | it failed 5 times in 10 minutes; fix the cause, then `sudo systemctl reset-failed orchestrator` and start it. |

To see the error directly, stop the service and run it in the foreground without sending anything to
the boiler Pi (the secrets file is root-only, so the secrets are not loaded in this test):

```
sudo systemctl stop orchestrator
cd /opt/orchestrator/current
sudo -u orchestrator env DRY_RUN=1 ORCHESTRATOR_CONFIG=/etc/orchestrator/config.toml venv/bin/python -m orchestrator
```

Stop it with Ctrl-C. If you cannot find the cause, send me the output of the `journalctl` command above.

## 6. Optional

**Reboot of the boiler Pi by e-mail (`reset`).** The reboot is refused until the boiler Pi's SSH host
key is known:

```
cd /opt/orchestrator/current
sudo -u orchestrator venv/bin/python -m orchestrator.ssh_reboot --show-host-key --config /etc/orchestrator/config.toml
```

Compare the printed fingerprint with the output of `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`
run on the boiler Pi, then copy it to `[boiler] ssh_host_key` and restart. The SSH user on the boiler Pi
needs `sudo reboot` without a password.

**Editing schedules in the page.** The old calendar login is read-only. Make one sign-in with the
full calendar permission and write it over the calendar token (on a Pi without a browser add
`--no-browser` and use `ssh -L 8765:localhost:8765 pi@<pi>` from your PC):

```
cd /opt/orchestrator/current
sudo -u orchestrator venv/bin/python -m orchestrator.auth \
    --client-secret /var/lib/orchestrator/google/client_secret.json \
    --token /var/lib/orchestrator/google/calendar_token.json --port 8765
```

`--port` is the port on which the tool waits for Google's answer for a minute or two. Do not use 8080:
the web page of the running service has it.

Then set `[calendar] write_back = true` and restart. Try it on a separate test calendar first
(`[calendar] calendar_id`): edits change the whole weekly series in the real calendar.

## 7. Before you rely on it

* Do a supervised test against the boiler Pi: a schedule push, manual On / Off / Auto, a relay run,
  and what happens when the boiler Pi is switched off. `DRY_RUN=1` first if you want to see the files
  built but not sent.
* Run the one-hour measurement while the service runs:
  `cd ~/orchestrator-src && python3 -m tools.perf_check --duration 3600` (the tool is not part of
  the installed program; it only uses the Python standard library). It prints PASS or FAIL for the
  memory, CPU and response-time limits.
* Look at the page on a phone.
* Once everything works, delete the old Google files from the source folder on the Pi.

## Updating and going back

Copy the new version over and run `sudo bash deploy/install.sh` again. Configuration, secrets and data
are kept. The service is stopped while the new program installs (a few minutes; the heating is not
affected). If the new program does not start, the old one is put back by itself. To go back later:

```
sudo bash deploy/rollback.sh
```

Run it again to swap forward.

## Uninstalling

The boiler Pi is not affected: it keeps running on the last schedule files it was sent. If you want a
manual override cleared first, switch to **Auto** in the page before uninstalling.

**1. Stop and remove the service and the program** (keeps your settings and data):

```
sudo systemctl disable --now orchestrator
sudo rm /etc/systemd/system/orchestrator.service
sudo systemctl daemon-reload
sudo rm -r /opt/orchestrator
```

**2. Remove everything else** (only if you will not reinstall; this deletes the settings, the secrets
file, the Google logins, the saved schedule files and the logs):

```
sudo rm -r /etc/orchestrator /var/lib/orchestrator /var/log/orchestrator
sudo userdel orchestrator
```

**3. If you installed with `--volatile-journal`**, undo it:

```
sudo rm -f /etc/systemd/journald.conf.d/orchestrator.conf
sudo systemctl restart systemd-journald
```

**4. Clean up the copy you installed from**, which holds your old Google secrets:

```
rm -r ~/orchestrator-src ~/orchestrator-pi.tgz
```

Also remove the Home Assistant entities if you used MQTT (Settings, Devices, "Home Control
Orchestrator", Delete) and, if you want to cut Google's access, revoke the app in your Google
account under Security, "Third-party access".
