# Architecture: code and flow

Diagrams are in [Mermaid](https://mermaid.js.org/): they show as pictures on GitHub and in VS Code
(with a Mermaid preview extension). A test (`tests/test_docs.py`) checks that every module of the
`orchestrator` package is named here, so this page cannot silently go out of date.

## 1. The system around the Orchestrator

```mermaid
flowchart LR
    subgraph People
        browser["Browser: page on a PC or phone"]
        mailer["E-mail: commands and alerts"]
        ha["Home Assistant"]
    end
    subgraph Google
        cal["Google Calendar: events are the schedules"]
        gmail["Gmail: command mailbox, outgoing mail"]
    end
    orch(["Orchestrator on a Pi 3"])
    boiler["Boiler Pi: REST API port 8000<br/>reads Cal.json, TimeCal.json, manual.json"]
    heater["Heater and relays"]

    browser <-->|"HTTP 8080: JSON API and one static page"| orch
    mailer <-->|"commands in, notices and alerts out"| gmail
    gmail <-->|"Gmail API"| orch
    cal <-->|"Calendar API: read, optional write-back"| orch
    ha <-->|"MQTT: state, discovery, commands"| orch
    orch -->|"POST schedule and manual files"| boiler
    boiler -->|"GET status, history, log"| orch
    orch -.->|"SSH: sudo reboot only"| boiler
    boiler --> heater
```

The boiler Pi keeps working from its last files whether the Orchestrator runs or not. The
Orchestrator only uses its REST interface (and SSH for the `reset` command).

## 2. Code map: modules by layer

```mermaid
flowchart TB
    subgraph entry["Start and wiring"]
        main["__main__ / app: builds everything, starts it in the order of OPS-2"]
        config["config: TOML plus secrets from the environment"]
        logging_setup["logging_setup: logs, redaction, audit, correlation ids"]
        clock["clock: time zone, injectable clock"]
    end

    subgraph core["Pure logic: no network, no threads"]
        models["models: slots, entries, status, history"]
        weekdays["weekdays: day 1 = Sunday, expiry time"]
        schedule_files["schedule_files: byte-exact writer and reader"]
        validation["validation: limit of 5 showers, rules"]
        manual["manual: manual.json and relay run files"]
        temperatures["temperatures: fixed sensor positions"]
        relays["relays: relay names"]
        event_titles["event_titles: grammar of calendar titles"]
        astro["astro: sunrise, sunset with the event date's offset"]
        files["files: atomic writes"]
    end

    subgraph boilerside["Boiler Pi side"]
        boiler_client["boiler_client: REST client, DRY_RUN"]
        status_cache["status_cache: poller and locked cache"]
        push_queue["push_queue: change detection, retry, gate"]
        manual_commands["manual_commands: On, Off, Auto, relay run"]
    end

    subgraph calendar["Calendar side"]
        calendar_source["calendar_source: reads events"]
        calendar_schedules["calendar_schedules: events to schedules"]
        calendar_sync["calendar_sync: the periodic read"]
        calendar_writer["calendar_writer: create, change, delete events"]
        schedule_editor["schedule_editor: edit rules, conflicts"]
        google_auth["google_auth and auth: tokens, sign-in tool"]
    end

    subgraph channels["Front ends over the same commands"]
        web_api["web_api, web_input, ui/index.html"]
        mqtt_bridge["mqtt_bridge: Home Assistant"]
        mail_client["mail_client: Gmail"]
        mail_gate["mail_gate: sender checks, alerts"]
        mail_commands["mail_commands: 2-step token flow"]
        mail_worker["mail_worker: inbox loop"]
        ssh_reboot["ssh_reboot and reboot_grace"]
    end

    subgraph ops["Operation"]
        supervisor["supervisor: health, watchdog, healthz"]
        system_facts["system_facts: memory, disk, temperature, clock"]
        diagnostics["diagnostics: resource alerts, heartbeat"]
        admin_notifier["admin_notifier: mails to the admin"]
    end

    main --> config
    main --> logging_setup
    main --> clock
    main --> boilerside
    main --> calendar
    main --> channels
    main --> ops

    calendar_source --> calendar_schedules
    event_titles --> calendar_schedules
    astro --> calendar_schedules
    calendar_schedules --> calendar_sync
    calendar_sync --> push_queue
    schedule_editor --> calendar_writer
    google_auth --> calendar_source
    google_auth --> mail_client

    push_queue --> schedule_files
    push_queue --> boiler_client
    status_cache --> boiler_client
    manual_commands --> manual
    manual_commands --> boiler_client
    manual_commands --> status_cache

    web_api --> manual_commands
    web_api --> schedule_editor
    web_api --> status_cache
    mqtt_bridge --> manual_commands
    mail_commands --> manual_commands
    mail_worker --> mail_gate
    mail_worker --> mail_commands
    mail_commands --> ssh_reboot
    mail_gate --> mail_client
    mail_commands --> mail_client

    schedule_files --> models
    validation --> models
    manual --> models
    temperatures --> models
    models --> weekdays

    supervisor --> system_facts
    diagnostics --> admin_notifier
    admin_notifier --> mail_client
```

Rules that keep it simple:

* The **pure logic** layer imports nothing from the other layers.
* Every front end (page, MQTT, e-mail) calls the **same** `manual_commands` object, so the 5-shower
  limit, relays 2 to 7 only, and the audit log apply to all of them.
* Only `push_queue` and `manual_commands` send files to the boiler Pi, only through `boiler_client`.
* Nothing is done at import time and dependencies are passed in, so everything is tested with fakes.

## 3. What runs when the service is up

```mermaid
flowchart LR
    subgraph threads["Threads"]
        poller["status poller<br/>every 10 s"]
        pusher["push queue<br/>wakes on change, retries"]
        sync["calendar sync<br/>every 5 min or on request"]
        mailw["mail check<br/>every 30 s"]
        mqttw["mqtt worker<br/>plus the client's network thread"]
        diag["diagnostics<br/>every 60 s"]
        web["web server: waitress, a few threads"]
        sup["supervisor<br/>every 5 s"]
    end
    cache[("status cache<br/>locked")]
    files[("schedule files on disk<br/>Cal.json, TimeCal.json, push state")]
    report[("last calendar report")]
    health[("last health report")]

    poller -->|writes| cache
    sync -->|writes| report
    sync -->|"new schedules"| pusher
    pusher <-->|reads and writes| files
    web -->|"reads only"| cache
    web -->|"reads only"| report
    web -->|reads| health
    mqttw -->|reads| cache
    sup -->|"writes, pings systemd watchdog"| health
    sup -.->|"restarts a dead worker"| threads
    diag -->|reads| health
```

HTTP handlers only read the shared state, except actions (manual commands, schedule edits), which
call the command objects, and the 24 h history, which is fetched on demand and kept for a minute.

## 4. Flow A: a calendar event becomes a heating schedule

```mermaid
sequenceDiagram
    participant G as Google Calendar
    participant S as calendar_sync
    participant C as calendar_schedules
    participant P as push_queue
    participant B as Boiler Pi
    participant N as admin_notifier

    S->>G: list events of this week
    G-->>S: events, recurring ones expanded
    S->>C: build schedules
    Note over C: parse each title, resolve sunrise or sunset<br/>with the offset of the event date,<br/>skip and report bad events, limit of 5 showers
    C-->>S: boiler schedule, timer schedule, problems
    S->>P: submit both (empty schedules are refused)
    P->>P: serialize byte-exact, compare with the file on disk
    alt content changed
        P->>P: write the file atomically
        P->>B: POST file (multipart field "file")
        B-->>P: HTTP 200
        S->>N: calendar changed, with the difference
    else unchanged
        Note over P: nothing is written or sent
    end
    alt push fails
        P->>P: retry with backoff, keep the file, alert after 15 minutes
    end
```

## 5. Flow B: manual On, Off, Auto (page, MQTT or e-mail)

```mermaid
sequenceDiagram
    actor U as User
    participant F as Page, MQTT or e-mail
    participant M as manual_commands
    participant K as status cache
    participant B as Boiler Pi

    U->>F: On
    F->>M: manual_on(source, who)
    M->>K: fresh status?
    alt status unknown or old, or boiler down
        M-->>F: refused with the reason, nothing sent
    else
        M->>M: level = max(showers) + margin, at most 5.<br/>expiry = now + 3 h as weekday and time
        M->>B: POST manual.json once, never retried
        M->>B: GET status
        B-->>M: Status = Manual
        M-->>F: done, the boiler Pi reports Manual
    end
    F-->>U: the real outcome
    Note over B: the boiler Pi deletes manual.json at the expiry time<br/>and the schedule applies again. Auto uploads "[]".
```

## 6. Flow C: an e-mail command with two-step confirmation

```mermaid
sequenceDiagram
    actor U as Authorized user
    participant W as mail_worker
    participant X as mail_gate
    participant C as mail_commands
    participant M as manual_commands
    participant A as security alert

    U->>W: mail "off"
    W->>X: classify (sender list, SPF or DKIM, own or automatic mail)
    alt not accepted
        X->>A: alert to the alert address, rate limited, no reply to the sender
    else accepted
        W->>C: handle
        C-->>U: reply with a random 10-letter token as subject, nothing has changed
        U->>W: reply with the token within 120 s
        W->>C: handle (same sender, once)
        alt token correct
            C->>M: manual_off(source "email")
            C-->>U: mail with the real outcome
        else wrong or late
            C->>A: alert, count the failure (5 failures lock commands for 15 minutes)
        end
    end
    Note over W: every mail looked at is moved to the trash afterwards
```

## 7. Flow D: editing a schedule in the page

```mermaid
sequenceDiagram
    actor U as User
    participant P as Page
    participant E as schedule_editor
    participant W as calendar_writer
    participant G as Google Calendar
    participant S as calendar_sync

    U->>P: save a slot
    P->>E: create or update (checked: 5 showers, title must read back identically)
    alt write-back off, read-only login, or an event type that cannot be edited
        E-->>P: refused with the reason
    else
        E->>W: write the weekly event
        W->>G: insert or patch
        E->>S: request an early read
        S->>G: read events
        Note over S: Flow A: the change reaches the boiler Pi
        E-->>P: saved, the list refreshes
    end
```

## 8. Flow E: start-up and supervision

```mermaid
flowchart TB
    start(["systemd starts: python -m orchestrator"]) --> cfg["load and check the configuration<br/>a mistake stops with a clear message"]
    cfg --> clk["wait up to 2 minutes for the clock<br/>until then nothing is sent to the boiler Pi"]
    clk --> load["load the last schedule files from disk"]
    load --> google["build Google access from the token files<br/>missing or refused: degraded, not stopped"]
    google --> workers["start the workers: poller, push queue,<br/>calendar, mail, MQTT, diagnostics"]
    workers --> web["start the web server"]
    web --> ready["supervisor sees the web server answer<br/>and tells systemd READY"]
    ready --> tick{{"every 5 seconds"}}
    tick --> check["each worker: finished a cycle within 5 times its interval?<br/>thread alive?"]
    check -->|"yes"| ping["ping the systemd watchdog"]
    check -->|"dead thread"| restart["restart it, at most 5 times an hour"]
    check -->|"critical worker failed"| nop["no ping: systemd restarts the service"]
    check -->|"outside world in trouble"| degraded["mark degraded, never restart"]
    ping --> tick
    restart --> tick
    degraded --> tick
```

`/healthz` answers from the last check: 200 for ok or degraded, 503 for failed.

## 9. Where to look

| I want to change... | Start in |
|---|---|
| the format of the files sent to the boiler Pi | `schedule_files`, then `models` |
| what a calendar title means | `event_titles`, `calendar_schedules` |
| manual On/Off rules or the 5-shower limit | `manual`, `validation`, `manual_commands` |
| the web page | `ui/index.html` (one file), `web_api`, `web_input` |
| who may send e-mail commands | `mail_gate`, `mail_commands` |
| Home Assistant entities | `mqtt_bridge` |
| alerts and health | `admin_notifier`, `diagnostics`, `supervisor` |
| how it is installed | `deploy/`, `docs/INSTALL.md` |
