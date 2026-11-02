"""The web API of the UI (FR-UI-1..7, CS-15, OPS-8).

``create_app`` builds a Flask app over the objects the rest of the program already has. The handlers
only read the shared caches (status, last calendar report, push statuses, ring buffer) or call the
same command objects the other channels use; nothing here talks to the boiler Pi directly, except
the 24 h history, which is fetched on demand and kept for a short time.

Safety of the interface: JSON only, a small body limit, a same-origin check on every request that
changes something, an optional login (failed attempts are rate limited) and no secret in any
answer. Reading needs no login (FR-UI-7: the password protects edit and manual actions).
"""

import gzip
import hmac
import importlib
import logging
import platform
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, session
from werkzeug.exceptions import HTTPException

from orchestrator import __version__
from orchestrator.calendar_schedules import ScheduleItem
from orchestrator.calendar_sync import CalendarSync, SyncReport
from orchestrator.clock import Clock
from orchestrator.config import Config
from orchestrator.event_titles import AstroTime, BoilerCommand, TimerCommand
from orchestrator.files import sha256_hex
from orchestrator.logging_setup import kv
from orchestrator.manual_commands import CommandResult, ManualCommands
from orchestrator.models import (
    BoilerStatus,
    HistorySample,
    format_number,
    format_time,
)
from orchestrator.push_queue import PushResult, PushStatus
from orchestrator.relays import RelayNames
from orchestrator.schedule_editor import EditOutcome, EditResult, ScheduleEditor
from orchestrator.status_cache import StatusCache
from orchestrator.temperatures import levels_of
from orchestrator.web_input import (
    BOILER_FIELDS,
    TIMER_FIELDS,
    InputError,
    check_fields,
    parse_boiler,
    parse_clock,
    parse_days,
    parse_edited_at,
    parse_timer,
    require_object,
    whole_number,
)
from orchestrator.weekdays import day_and_time, day_name

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024
MAX_FAILED_LOGINS = 5
LOGIN_WINDOW_S = 300.0
HISTORY_TTL_S = 60.0
_OUTCOME_STATUS = {
    EditOutcome.APPLIED: 200,
    EditOutcome.REFUSED: 422,
    EditOutcome.CONFLICT: 409,
    EditOutcome.FAILED: 502,
}


class LoginLimiter:
    """Allows ``MAX_FAILED_LOGINS`` wrong passwords per address in ``LOGIN_WINDOW_S`` seconds."""

    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, who: str) -> list[float]:
        cutoff = self._monotonic() - LOGIN_WINDOW_S
        recent = [moment for moment in self._failures.get(who, []) if moment > cutoff]
        if recent:
            self._failures[who] = recent
        else:
            self._failures.pop(who, None)
        return recent

    def blocked(self, who: str) -> bool:
        with self._lock:
            return len(self._recent(who)) >= MAX_FAILED_LOGINS

    def failed(self, who: str) -> None:
        with self._lock:
            self._recent(who)
            self._failures.setdefault(who, []).append(self._monotonic())

    def succeeded(self, who: str) -> None:
        with self._lock:
            self._failures.pop(who, None)


class HistoryProvider:
    """The 24 h history, fetched when asked for and reused for ``HISTORY_TTL_S`` seconds."""

    def __init__(
        self, fetch: Callable[[], tuple[HistorySample, ...]], monotonic: Callable[[], float]
    ) -> None:
        self._fetch = fetch
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._samples: tuple[HistorySample, ...] = ()
        self._loaded_at: float | None = None
        self.error: str | None = None

    def samples(self) -> tuple[HistorySample, ...]:
        with self._lock:
            now = self._monotonic()
            if self._loaded_at is None or now - self._loaded_at >= HISTORY_TTL_S:
                try:
                    self._samples = self._fetch()
                    self.error = None
                except Exception as error:  # the chart is optional; keep the last good samples
                    self.error = type(error).__name__
                    logger.warning(
                        kv("history_failed", error=type(error).__name__, detail=str(error))
                    )
                self._loaded_at = now
            return self._samples


@dataclass(frozen=True, slots=True)
class WebDeps:
    """Everything the API needs; built once at start-up (and by the tests)."""

    config: Config
    clock: Clock
    cache: StatusCache
    manual: ManualCommands
    history: HistoryProvider
    sync: CalendarSync | None = None
    editor: ScheduleEditor | None = None
    push_statuses: Callable[[], list[PushStatus]] = lambda: []
    log_lines: Callable[[], list[str]] = lambda: []
    relay_names: RelayNames = field(default_factory=RelayNames)
    health: Callable[[], dict[str, Any]] = lambda: {"status": "ok"}
    push_now: Callable[[], list[PushResult]] | None = None


UI_FILE = Path(__file__).parent / "ui" / "index.html"
# the page has inline script and style (one file, no build step); nothing is loaded from elsewhere
PAGE_POLICY = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


@dataclass(frozen=True, slots=True)
class Page:
    """The UI page, read once: plain and gzipped bytes and the tag for conditional requests."""

    plain: bytes
    gzipped: bytes
    etag: str

    @classmethod
    def load(cls, path: Path = UI_FILE) -> "Page":
        plain = path.read_bytes()
        return cls(plain, gzip.compress(plain, compresslevel=9, mtime=0), sha256_hex(plain)[:16])


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _error(message: str, code: int) -> tuple[Response, int]:
    return jsonify(error=message), code


def create_app(deps: WebDeps) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.secret_key = secrets.token_bytes(32)  # sessions end when the service restarts
    limiter = LoginLimiter(deps.clock.monotonic)
    password = deps.config.secrets.ui_password
    login_required = deps.config.web.login_required
    started = deps.clock.monotonic()
    page = Page.load()

    @app.get("/")
    def index() -> Response:
        tag = f'"{page.etag}"'
        if request.headers.get("If-None-Match") == tag:
            response = Response(status=304)
        elif "gzip" in request.headers.get("Accept-Encoding", ""):
            response = Response(page.gzipped, mimetype="text/html")
            response.headers["Content-Encoding"] = "gzip"
        else:
            response = Response(page.plain, mimetype="text/html")
        response.headers["ETag"] = tag
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Vary"] = "Accept-Encoding"
        response.headers["Content-Security-Policy"] = PAGE_POLICY
        return response

    def who() -> str:
        return "ui:admin" if session.get("user") else f"ui:{request.remote_addr or 'unknown'}"

    # --- guards -----------------------------------------------------------------------------

    @app.before_request
    def guard() -> tuple[Response, int] | None:
        if request.method in ("GET", "HEAD"):
            return None
        if request.mimetype != "application/json":
            return _error("send JSON (Content-Type: application/json)", 415)
        origin = request.headers.get("Origin")
        if origin is not None and urlsplit(origin).netloc != request.host:
            logger.warning(kv("csrf_refused", path=request.path))
            return _error("cross-site request refused", 403)
        if login_required and request.path != "/api/login" and not session.get("user"):
            return _error("login required", 401)
        return None

    @app.after_request
    def headers(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_problem(error: HTTPException) -> tuple[Response, int]:
        return _error((error.name or "error").lower(), error.code or 500)

    @app.errorhandler(InputError)
    def bad_input(error: InputError) -> tuple[Response, int]:
        return _error(str(error), 400)

    @app.errorhandler(Exception)
    def unexpected(error: Exception) -> tuple[Response, int]:
        logger.exception(kv("api_error", path=request.path, error=type(error).__name__))
        return _error("internal error", 500)

    def body() -> Any:
        data = request.get_json(silent=True)
        if data is None:
            raise InputError("the request body is not valid JSON")
        return require_object(data)

    # --- login ------------------------------------------------------------------------------

    @app.post("/api/login")
    def login() -> tuple[Response, int]:
        address = request.remote_addr or "unknown"
        if password is None:
            return _error("login is not enabled", 404)
        if limiter.blocked(address):
            logger.warning(kv("login_blocked", address=address))
            return _error("too many failed attempts; wait a few minutes", 429)
        data = body()
        check_fields(data, ("password",))
        given = data.get("password")
        if isinstance(given, str) and hmac.compare_digest(
            given.encode("utf-8"), password.encode("utf-8")
        ):
            limiter.succeeded(address)
            session["user"] = "admin"
            logger.info(kv("login_ok", address=address))
            return jsonify(ok=True), 200
        limiter.failed(address)
        logger.warning(kv("login_failed", address=address))
        return _error("wrong password", 401)

    @app.post("/api/logout")
    def logout() -> tuple[Response, int]:
        session.clear()
        return jsonify(ok=True), 200

    @app.get("/api/session")
    def session_info() -> Response:
        return jsonify(
            login_required=login_required,
            logged_in=bool(session.get("user")) or not login_required,
        )

    # --- reading ----------------------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> tuple[Response, int]:
        report = deps.health()  # the last supervisor check: cheap, no login (OPS-8)
        return jsonify(report), 503 if report.get("status") == "failed" else 200

    @app.get("/api/status")
    def status() -> Response:
        snapshot = deps.cache.snapshot()
        limit = timedelta(seconds=deps.config.boiler.stale_after_s)
        now = deps.clock.now()
        reading = snapshot.status
        return jsonify(
            connection={
                "state": snapshot.state.value,
                "error": snapshot.error,
                "failures": snapshot.consecutive_failures,
                "fetched_at": _iso(snapshot.fetched_at),
                "unhealthy_since": _iso(snapshot.unhealthy_since),
            },
            stale=snapshot.is_stale(now, limit),
            now=now.isoformat(),
            dry_run=deps.config.general.dry_run,
            status=_status_json(reading, now) if reading else None,
        )

    @app.get("/api/history")
    def history() -> Response:
        samples = deps.history.samples()
        return jsonify(
            error=deps.history.error,
            samples=[
                {
                    "at": sample.at.isoformat(),
                    "showers_linear": sample.showers_linear,
                    "showers_thermo": sample.showers_thermo,
                    "min": sample.minimum,
                    "target": sample.target,
                    "heater": sample.heater,
                }
                for sample in samples
            ],
        )

    @app.get("/api/schedules")
    def schedules() -> Response:
        report = deps.sync.latest() if deps.sync else None
        return jsonify(
            editing_enabled=deps.editor is not None and deps.editor.enabled,
            calendar=_report_json(report),
            push=[_push_json(item) for item in deps.push_statuses()],
            boiler=_slots_json(report),
            timer=_entries_json(report),
            items=_items_json(report),
        )

    @app.get("/api/relays")
    def relays() -> Response:
        return jsonify(
            relays=[
                {"number": n, "name": deps.relay_names.name_of(n), "locked": n < 2}
                for n in range(8)
            ]
        )

    @app.get("/api/system")
    def system() -> Response:
        snapshot = deps.cache.snapshot()
        report = deps.sync.latest() if deps.sync else None
        return jsonify(
            version=__version__,
            python=platform.python_version(),
            uptime_s=int(deps.clock.monotonic() - started),
            dry_run=deps.config.general.dry_run,
            connection=snapshot.state.value,
            calendar=_report_json(report),
            push=[_push_json(item) for item in deps.push_statuses()],
            log=deps.log_lines()[-200:],
            health=deps.health(),
        )

    # --- manual control ---------------------------------------------------------------------

    @app.post("/api/manual")
    def manual() -> tuple[Response, int]:
        data = body()
        check_fields(data, ("action",))
        action = data.get("action")
        if action == "on":
            result = deps.manual.manual_on(source="ui", who=who())
        elif action == "off":
            result = deps.manual.manual_off(source="ui", who=who())
        elif action == "auto":
            result = deps.manual.auto(source="ui", who=who())
        else:
            raise InputError("action: expected on, off or auto")
        return _command_json(result)

    @app.post("/api/relays/<int:number>")
    def relay(number: int) -> tuple[Response, int]:
        data = body()
        check_fields(data, ("action", "seconds"))
        action = data.get("action")
        if action == "on":
            seconds = whole_number(data.get("seconds"), "seconds", -86400, 86400)
            result = deps.manual.relay_on(number, seconds, source="ui", who=who())
        elif action == "off":
            seconds = whole_number(data.get("seconds", 0), "seconds", -86400, 86400)
            result = deps.manual.relay_off(number, seconds, source="ui", who=who())
        else:
            raise InputError("action: expected on or off")
        return _command_json(result)

    # --- schedule editing -------------------------------------------------------------------

    @app.post("/api/schedules/<kind>")
    def create_item(kind: str) -> tuple[Response, int]:
        editor = _editor()
        data = body()
        command, event_time = _command_from(kind, data)
        days = parse_days(data.get("days"))
        start = deps.clock.now().replace(
            hour=event_time.hour, minute=event_time.minute, second=event_time.second, microsecond=0
        )
        return _edit_json(editor.create(command, start, days, who=who()))

    @app.put("/api/schedules/<kind>/<path:item_id>")
    def update_item(kind: str, item_id: str) -> tuple[Response, int]:
        editor = _editor()
        data = body()
        item = _find(kind, item_id)
        command, event_time = _command_from(kind, data, time_required=False)
        days = parse_days(data["days"]) if "days" in data else None
        result = editor.update(
            item,
            command,
            who=who(),
            start_time=event_time if "time" in data else None,
            days=days,
            edited_at=parse_edited_at(data.get("edited_at")),
        )
        return _edit_json(result)

    @app.delete("/api/schedules/<kind>/<path:item_id>")
    def delete_item(kind: str, item_id: str) -> tuple[Response, int]:
        editor = _editor()
        data = body()
        check_fields(data, ("edited_at",))
        item = _find(kind, item_id)
        return _edit_json(
            editor.delete(item, who=who(), edited_at=parse_edited_at(data.get("edited_at")))
        )

    @app.post("/api/push")
    def push() -> tuple[Response, int]:
        if deps.push_now is None:
            return _error("pushing is not available", 503)
        results = deps.push_now()
        logger.info(kv("push_now", who=who(), results=len(results)))
        return (
            jsonify(
                ok=all(r.error is None for r in results),
                results=[
                    {"kind": r.kind.value, "outcome": r.outcome.value, "error": r.error}
                    for r in results
                ],
            ),
            200,
        )

    @app.post("/api/sync")
    def sync_now() -> tuple[Response, int]:
        if deps.sync is None:
            return _error("the calendar is not configured", 503)
        deps.sync.request_sync()
        return jsonify(ok=True), 202

    # --- helpers that need the dependencies -------------------------------------------------

    def _editor() -> ScheduleEditor:
        if deps.editor is None:
            raise _Unavailable
        return deps.editor

    def _find(kind: str, item_id: str) -> ScheduleItem:
        _check_kind(kind)
        report = deps.sync.latest() if deps.sync else None
        for item in report.schedules.items if report and report.schedules else ():
            if item.event.event_id == item_id and _kind_of(item) == kind:
                return item
        raise _Missing

    def _command_from(
        kind: str, data: Any, *, time_required: bool = True
    ) -> tuple[BoilerCommand | TimerCommand, Any]:
        _check_kind(kind)
        if kind == "boiler":
            check_fields(data, BOILER_FIELDS)
            needed = time_required or "time" in data
            return parse_boiler(data), parse_clock(data.get("time"), "time") if needed else None
        check_fields(data, TIMER_FIELDS)
        if time_required and "time" not in data and "start_astro" not in data:
            raise InputError("time: required (or give start_astro)")
        timer = parse_timer(data)
        return timer.command, timer.event_time

    @app.errorhandler(_Unavailable)
    def unavailable(_error_: object) -> tuple[Response, int]:
        return _error("editing is not available", 503)

    @app.errorhandler(_Missing)
    def missing(_error_: object) -> tuple[Response, int]:
        return _error("that schedule item is not in the latest calendar reading; reload", 404)

    return app


class WebServer(Protocol):
    """The part of a waitress server that the supervisor uses."""

    effective_port: int

    def run(self) -> None: ...

    def close(self) -> None: ...


class _WaitressServer:
    """A waitress server that can be stopped from another thread without errors.

    Waitress closes its sockets at once on ``close()``, while a worker thread may still be
    finishing a request and a select call may still be waiting on them; both end in an
    ``OSError`` in some thread. Stopping the workers first and then the sockets avoids the first,
    and the stop flag makes the loop's own error on the closed socket an ordinary exit.
    """

    def __init__(self, server: Any) -> None:
        self._server = server
        self._closing = False
        self.effective_port: int = server.effective_port

    def run(self) -> None:
        try:
            self._server.run()
        except (OSError, ValueError):
            if not self._closing:
                raise

    def close(self) -> None:
        self._closing = True
        self._server.task_dispatcher.shutdown()
        self._server.close()
        # connections that a browser keeps open stay in waitress's socket table after close();
        # close them too, or each one is only released (with a ResourceWarning) by the collector
        channels = getattr(self._server, "_map", None)
        if channels is not None:
            importlib.import_module("waitress.wasyncore").close_all(channels)


def make_server(app: Flask, host: str, port: int, *, threads: int = 4) -> WebServer:
    """A waitress server for the app (OPS-8); few threads keep the Pi 3's memory use small."""
    waitress = importlib.import_module("waitress")
    server = waitress.create_server(
        app,
        host=host,
        port=port,
        threads=threads,
        max_request_body_size=MAX_BODY_BYTES,
        ident="orchestrator",
        clear_untrusted_proxy_headers=True,
    )
    return _WaitressServer(server)


class _Unavailable(Exception):
    pass


class _Missing(Exception):
    pass


def _check_kind(kind: str) -> None:
    if kind not in ("boiler", "timer"):
        raise _Missing


def _kind_of(item: ScheduleItem) -> str:
    return "boiler" if isinstance(item.command, BoilerCommand) else "timer"


def _status_json(status: BoilerStatus, now: datetime) -> dict[str, Any]:
    return {
        "mode": status.mode,
        "heater_on": status.heater_on,
        "active": status.active,
        "showers_linear": status.showers_linear,
        "showers_thermo": status.showers_thermo,
        "levels": levels_of(status).as_dict(),
        "target": status.target,
        "min": status.minimum,
        "minutes_to_start": status.minutes_to_start,
        "date": status.date.isoformat(),
        "age_s": int(status.age(now).total_seconds()),
    }


def _command_json(result: CommandResult) -> tuple[Response, int]:
    payload = {
        "ok": result.ok,
        "sent": result.sent,
        "message": result.message,
        "observed_mode": result.observed_mode,
        "correlation_id": result.correlation_id,
    }
    return jsonify(payload), 200 if result.ok else 409


def _edit_json(result: EditResult) -> tuple[Response, int]:
    payload = {
        "outcome": result.outcome.value,
        "message": result.message,
        "event_id": result.event_id,
        "correlation_id": result.correlation_id,
    }
    return jsonify(payload), _OUTCOME_STATUS[result.outcome]


def _report_json(report: SyncReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "at": report.at.isoformat(),
        "ok": report.ok,
        "error": report.error,
        "auth_lost": report.auth_lost,
        "problems": list(report.problems),
        "diff": report.diff,
    }


def _push_json(item: PushStatus) -> dict[str, Any]:
    return {
        "kind": item.kind.value,
        "pending": item.pending,
        "has_local_file": item.has_local_file,
        "last_pushed_at": _iso(item.last_pushed_at),
        "last_error": item.last_error,
        "attempts": item.attempts,
        "next_attempt_at": _iso(item.next_attempt_at),
    }


def _slots_json(report: SyncReport | None) -> list[dict[str, Any]]:
    if report is None or report.schedules is None:
        return []
    return [
        {
            "day": slot.day,
            "day_name": day_name(slot.day),
            "start": format_time(slot.start),
            "target": format_number(slot.target),
            "min": format_number(slot.minimum),
        }
        for slot in report.schedules.boiler.slots
    ]


def _entries_json(report: SyncReport | None) -> list[dict[str, Any]]:
    if report is None or report.schedules is None:
        return []
    return [
        {
            "day": entry.day,
            "day_name": day_name(entry.day),
            "at": format_time(entry.at),
            "state": entry.state,
            "relay": entry.relay,
        }
        for entry in report.schedules.timer.entries
    ]


def _items_json(report: SyncReport | None) -> list[dict[str, Any]]:
    if report is None or report.schedules is None:
        return []
    series_days: dict[str, set[int]] = {}
    for item in report.schedules.items:
        series_days.setdefault(_series_of(item), set()).add(day_and_time(item.event.start)[0])
    items: list[dict[str, Any]] = []
    for item in report.schedules.items:
        day, at = day_and_time(item.event.start)
        entry: dict[str, Any] = {
            "id": item.event.event_id,
            "kind": _kind_of(item),
            "title": item.event.summary,
            "day": day,
            "day_name": day_name(day),
            "time": format_time(at),
            "updated": _iso(item.event.updated),
            "series": _series_of(item),
            "series_days": sorted(series_days[_series_of(item)]),
        }
        command = item.command
        if isinstance(command, BoilerCommand):
            entry["target"] = format_number(command.target)
            entry["min"] = format_number(command.minimum)
        else:
            entry["relays"] = list(command.relays)
            entry["start_astro"] = _astro_json(command.start)
            entry["end"] = format_time(command.end_clock) if command.end_clock else None
            entry["end_astro"] = _astro_json(command.end_astro)
            entry["duration_s"] = command.duration_s
        items.append(entry)
    return items


def _series_of(item: ScheduleItem) -> str:
    return item.event.recurring_event_id or item.event.event_id


def _astro_json(value: AstroTime | None) -> dict[str, Any] | None:
    return None if value is None else {"astro": value.name, "offset": value.offset_minutes}
