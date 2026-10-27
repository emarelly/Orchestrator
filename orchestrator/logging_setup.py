"""Logging setup (LOG-1 .. LOG-12).

One call, :func:`setup_logging`, installs on the root logger:

* a console handler (systemd/journald),
* a rotating main log file,
* an in-memory ring buffer for the System tab of the UI,

and a separate rotating audit log on the ``orchestrator.audit`` logger. Every handler redacts
secrets (the configured passwords and anything that looks like ``password=...``, ``token: ...``
or an ``Authorization`` header) from the message *and* from tracebacks, and identical warnings
inside a window are collapsed into one line with a repeat count (LOG-5).

Messages carry ``key=value`` context built with :func:`kv`; a correlation id set with
:func:`correlation` is appended to every line produced inside the ``with`` block (LOG-9).
"""

import contextlib
import contextvars
import json
import logging
import logging.handlers
import platform
import re
import signal
import socket
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

from orchestrator import __version__
from orchestrator.config import Config

AUDIT_LOGGER_NAME = "orchestrator.audit"
HANDLER_PREFIX = "orchestrator-"
MASK = "***"
MIN_SECRET_LENGTH = 4
"""Shorter known secrets are not replaced by value (they would mangle ordinary text)."""

NOISY_LIBRARIES = (
    "urllib3",
    "googleapiclient",
    "google",
    "google_auth_oauthlib",
    "httplib2",
    "paramiko",
    "waitress",
    "werkzeug",
    "paho",
)

# ---------------------------------------------------------------------------------------------
# correlation id and key=value messages (LOG-3, LOG-9)
# ---------------------------------------------------------------------------------------------

_correlation: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


def new_correlation_id() -> str:
    """Return a short random id that ties together the lines of one action."""
    return uuid.uuid4().hex[:8]


def current_correlation_id() -> str | None:
    """Return the correlation id of the running action, if any."""
    return _correlation.get()


@contextlib.contextmanager
def correlation(correlation_id: str | None = None) -> Iterator[str]:
    """Tag every log line written inside the block with a correlation id."""
    value = correlation_id or new_correlation_id()
    token = _correlation.set(value)
    try:
        yield value
    finally:
        _correlation.reset(token)


def kv(event: str, /, **fields: object) -> str:
    """Build ``event=<event> key=value ...``; values with spaces or quotes are JSON-quoted.

    ``event`` is positional-only, so a field may itself be called ``event`` without a clash.
    """
    parts = [f"event={event}"]
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value)
        if not text or any(char.isspace() or char == '"' for char in text):
            text = json.dumps(text, ensure_ascii=False)
        parts.append(f"{key}={text}")
    return " ".join(parts)


def audit(event: str, /, **fields: object) -> None:
    """Record a state-changing action or security event in the audit log (LOG-8)."""
    logging.getLogger(AUDIT_LOGGER_NAME).info(kv(event, **fields))


# ---------------------------------------------------------------------------------------------
# redaction (LOG-7)
# ---------------------------------------------------------------------------------------------

_SECRET_WORD = (
    r"(?:password|passwd|pwd|passphrase|secret|token|authorization" r"|api[_-]?key|private[_-]?key)"
)
_KEY_VALUE = re.compile(rf"""(?ix)
    (?P<key>\b[\w.-]*{_SECRET_WORD}[\w.-]*)(?P<close>["']?)
    (?P<sep>\s*[=:]\s*)
    (?P<value>"[^"]*"|'[^']*'|(?:Bearer|Basic)\s+\S+|[^\s,;&]+)
    """)
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")


def _mask_key_value(match: re.Match[str]) -> str:
    key, sep, value = match.group("key"), match.group("sep"), match.group("value")
    plain = not value.startswith(("'", '"')) and not value.lower().startswith(("bearer", "basic"))
    # "UI_PASSWORD: required when ..." is an environment variable name in an error message,
    # not a key/value pair: upper-case names followed by ':' and a plain word are left alone.
    if plain and ":" in sep and "=" not in sep and key.isupper():
        return match.group(0)
    return f"{key}{match.group('close')}{sep}{MASK}"


class Redactor:
    """Replaces known secret values and secret-looking ``key=value`` pairs in text."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._lock = threading.Lock()
        self._secrets: list[str] = []
        for secret in secrets:
            self.add(secret)

    def add(self, secret: str | None) -> None:
        """Register one more value that must never be logged (for example a two-factor token)."""
        if not secret or len(secret) < MIN_SECRET_LENGTH:
            return
        with self._lock:
            if secret not in self._secrets:
                self._secrets.append(secret)
                self._secrets.sort(key=len, reverse=True)

    def __call__(self, text: str) -> str:
        with self._lock:
            secrets = list(self._secrets)
        for secret in secrets:
            text = text.replace(secret, MASK)
        text = _KEY_VALUE.sub(_mask_key_value, text)
        return _BEARER.sub(lambda match: f"{match.group(1)} {MASK}", text)


class RedactingFilter(logging.Filter):
    """Redacts the message, the traceback and the stack text of a record before it is formatted."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            message = str(record.msg)
        record.msg = self._redactor(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._redactor(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redactor(record.stack_info)
        return True


# ---------------------------------------------------------------------------------------------
# collapsing repeated warnings (LOG-5)
# ---------------------------------------------------------------------------------------------


class CollapseFilter(logging.Filter):
    """Lets an identical WARNING-or-worse message through once per window.

    Repeats inside the window are dropped and counted; the next occurrence after the window carries
    ``(repeated N more times in the last M min)``. INFO and DEBUG are never collapsed. Run it
    *after* the :class:`RedactingFilter` so messages are compared in their final form.
    """

    MAX_TRACKED = 500

    def __init__(self, window_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self._window = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, int, str], tuple[float, int]] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if self._window <= 0 or record.levelno < logging.WARNING:
            return True
        key = (record.name, record.levelno, str(record.msg))
        now = self._clock()
        with self._lock:
            previous = self._seen.get(key)
            if previous is not None and now - previous[0] < self._window:
                self._seen[key] = (previous[0], previous[1] + 1)
                return False
            if previous is not None and previous[1] > 0:
                minutes = (now - previous[0]) / 60
                count = previous[1]
                record.msg = (
                    f"{record.msg} (repeated {count} more times in the last {minutes:.0f} min)"
                )
            self._seen[key] = (now, 0)
            if len(self._seen) > self.MAX_TRACKED:
                oldest = min(self._seen, key=lambda item: self._seen[item][0])
                del self._seen[oldest]
        return True


# ---------------------------------------------------------------------------------------------
# formats (LOG-3)
# ---------------------------------------------------------------------------------------------


def _iso_time(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")


class LineFormatter(logging.Formatter):
    """One line: ISO time, level, logger, [thread], message and ``cid=...`` when set."""

    def format(self, record: logging.LogRecord) -> str:
        line = (
            f"{_iso_time(record)} {record.levelname:<8} {record.name} "
            f"[{record.threadName}] {record.getMessage()}"
        )
        correlation_id = current_correlation_id()
        if correlation_id:
            line += f" cid={correlation_id}"
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            line += "\n" + record.exc_text
        if record.stack_info:
            line += "\n" + self.formatStack(record.stack_info)
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line (optional format)."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, str] = {
            "time": _iso_time(record),
            "level": record.levelname,
            "logger": record.name,
            "thread": record.threadName or "",
            "message": record.getMessage(),
        }
        correlation_id = current_correlation_id()
        if correlation_id:
            entry["cid"] = correlation_id
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            entry["exception"] = record.exc_text
        return json.dumps(entry, ensure_ascii=False)


class RingBufferHandler(logging.Handler):
    """Keeps the last N formatted events in memory for the System tab (LOG-2)."""

    def __init__(self, capacity: int) -> None:
        super().__init__()
        self._events: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._events.append(self.format(record))
        except Exception:  # logging must never raise into the caller
            self.handleError(record)

    def snapshot(self) -> list[str]:
        """Return the buffered events, oldest first."""
        self.acquire()
        try:
            return list(self._events)
        finally:
            self.release()


# ---------------------------------------------------------------------------------------------
# installation
# ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class LoggingHandles:
    """What :func:`setup_logging` installed; keep it to read the ring buffer and to shut down."""

    ring: RingBufferHandler
    redactor: Redactor
    handlers: list[logging.Handler]
    audit_logger: logging.Logger

    def close(self) -> None:
        """Remove and close every handler this setup installed."""
        for handler in self.handlers:
            for logger in (logging.getLogger(), self.audit_logger):
                logger.removeHandler(handler)
            handler.close()
        self.handlers.clear()


def _remove_installed_handlers() -> None:
    for logger in (logging.getLogger(), logging.getLogger(AUDIT_LOGGER_NAME)):
        for handler in list(logger.handlers):
            if (handler.get_name() or "").startswith(HANDLER_PREFIX):
                logger.removeHandler(handler)
                handler.close()


def setup_logging(
    config: Config,
    *,
    stream: IO[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> LoggingHandles:
    """Install the handlers described in the module docstring (safe to call more than once)."""
    settings = config.logging
    log_dir = Path(config.general.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    redactor = Redactor(config.secrets.password_values())
    formatter: logging.Formatter = JsonFormatter() if settings.json_format else LineFormatter()

    _remove_installed_handlers()
    handlers: list[logging.Handler] = []

    def prepare(handler: logging.Handler, name: str, *, collapse: bool) -> logging.Handler:
        handler.set_name(HANDLER_PREFIX + name)
        handler.setFormatter(formatter)
        handler.addFilter(RedactingFilter(redactor))
        if collapse:
            handler.addFilter(CollapseFilter(settings.collapse_window_s, clock))
        handlers.append(handler)
        return handler

    root = logging.getLogger()
    root.setLevel(settings.level)
    if settings.console:
        root.addHandler(
            prepare(logging.StreamHandler(stream or sys.stderr), "console", collapse=True)
        )
    main_file = logging.handlers.RotatingFileHandler(
        log_dir / "orchestrator.log",
        maxBytes=settings.file_max_bytes,
        backupCount=settings.file_backup_count,
        encoding="utf-8",
        delay=True,
    )
    root.addHandler(prepare(main_file, "file", collapse=True))
    ring = RingBufferHandler(settings.ring_size)
    root.addHandler(prepare(ring, "ring", collapse=True))

    audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False
    audit_file = logging.handlers.RotatingFileHandler(
        log_dir / "audit.log",
        maxBytes=settings.audit_max_bytes,
        backupCount=settings.audit_backup_count,
        encoding="utf-8",
        delay=True,
    )
    audit_logger.addHandler(prepare(audit_file, "audit", collapse=False))

    for name in NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
    return LoggingHandles(ring, redactor, handlers, audit_logger)


# ---------------------------------------------------------------------------------------------
# run-time level change (LOG-10) and start-up banner (LOG-11)
# ---------------------------------------------------------------------------------------------


class DebugWindow:
    """Switches the root logger to DEBUG for a while and then back to the configured level."""

    def __init__(self, base_level: str, duration_s: float = 900.0) -> None:
        self._base_level = base_level
        self._duration = duration_s
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._timer is not None

    def toggle(self) -> bool:
        """Turn the DEBUG window on or off; returns True if DEBUG is now on."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
                logging.getLogger().setLevel(self._base_level)
                return False
            timer = threading.Timer(self._duration, self._expire)
            timer.daemon = True
            self._timer = timer
            logging.getLogger().setLevel(logging.DEBUG)
            timer.start()
            return True

    def _expire(self) -> None:
        with self._lock:
            self._timer = None
            logging.getLogger().setLevel(self._base_level)


def install_debug_signal(window: DebugWindow) -> bool:
    """Toggle the DEBUG window on ``SIGUSR1`` where the platform has it (the Pi, not Windows)."""
    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is None:
        return False
    signal.signal(sigusr1, lambda _signum, _frame: window.toggle())
    return True


def log_startup_banner(logger: logging.Logger, config: Config) -> None:
    """Log version, runtime and the configuration with secrets masked (LOG-11)."""
    logger.info(
        kv(
            "startup",
            version=__version__,
            python=platform.python_version(),
            host=socket.gethostname(),
            dry_run=config.general.dry_run,
        )
    )
    for section, values in config.describe().items():
        logger.info(kv("config", section=section, values=values))
