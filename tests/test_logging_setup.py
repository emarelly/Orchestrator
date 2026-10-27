"""Logging setup (LOG-1..LOG-12): format, redaction, rotation, ring buffer, audit, collapse."""

import dataclasses
import io
import json
import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from orchestrator import __version__
from orchestrator.config import Config, load_config
from orchestrator.logging_setup import (
    CollapseFilter,
    DebugWindow,
    LoggingHandles,
    Redactor,
    audit,
    correlation,
    current_correlation_id,
    kv,
    log_startup_banner,
    setup_logging,
)

SECRET = "S3cret-Value-4711"
UNREGISTERED = "unregistered-pw-9876"


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Give every test a clean root logger and put the original state back afterwards."""
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    audit_logger = logging.getLogger("orchestrator.audit")
    audit_state = (audit_logger.level, audit_logger.propagate)
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in list(audit_logger.handlers):
        audit_logger.removeHandler(handler)
        handler.close()
    root.setLevel(level)
    audit_logger.setLevel(audit_state[0])
    audit_logger.propagate = audit_state[1]


def make_config(tmp_path: Path, **logging_overrides: Any) -> Config:
    config = load_config(
        None,
        env={"UI_PASSWORD": SECRET, "MQTT_PASSWORD": SECRET, "BOILER_SSH_PASSWORD": SECRET},
    )
    return dataclasses.replace(
        config,
        general=dataclasses.replace(config.general, log_dir=str(tmp_path / "logs")),
        logging=dataclasses.replace(config.logging, **logging_overrides),
    )


def start(tmp_path: Path, **logging_overrides: Any) -> tuple[LoggingHandles, io.StringIO, Path]:
    stream = io.StringIO()
    handles = setup_logging(make_config(tmp_path, **logging_overrides), stream=stream)
    return handles, stream, tmp_path / "logs"


# --- key=value messages and correlation ids ---------------------------------------------------


def test_kv_builds_key_value_messages() -> None:
    assert kv("x", a=1, b="two words", c=None, d="") == 'event=x a=1 b="two words" d=""'


def test_a_field_may_itself_be_called_event_without_clashing_with_the_event_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # regression: kv("x", event=...) used to raise TypeError ("multiple values for event")
    expected = "event=schedule_edit event=abc123 result=ok"
    assert kv("schedule_edit", event="abc123", result="ok") == expected
    caplog.set_level(logging.INFO)
    audit("schedule_edit", event="abc123", result="ok")
    assert expected in caplog.text


def test_correlation_ids_nest_and_are_restored() -> None:
    assert current_correlation_id() is None
    with correlation("aaaa1111") as outer:
        assert outer == current_correlation_id() == "aaaa1111"
        with correlation() as inner:
            assert inner == current_correlation_id()
            assert inner != "aaaa1111"
        assert current_correlation_id() == "aaaa1111"
    assert current_correlation_id() is None


# --- format (LOG-3) ---------------------------------------------------------------------------


def test_line_format_has_time_level_logger_thread_message_and_correlation_id(
    tmp_path: Path,
) -> None:
    handles, stream, _ = start(tmp_path)
    with correlation("ab12cd34"):
        logging.getLogger("orchestrator.manual").info(kv("manual_on", source="ui", target=4.4))
    handles.close()
    line = stream.getvalue().strip()
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d INFO     orchestrator\.manual "
        r"\[\S+\] event=manual_on source=ui target=4\.4 cid=ab12cd34",
        line,
    ), line


def test_json_format_is_one_object_per_line(tmp_path: Path) -> None:
    handles, stream, _ = start(tmp_path, json_format=True)
    with correlation("ab12cd34"):
        logging.getLogger("orchestrator.x").warning("hello")
    handles.close()
    entry = json.loads(stream.getvalue().strip())
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "orchestrator.x"
    assert entry["message"] == "hello"
    assert entry["cid"] == "ab12cd34"
    assert entry["time"].count("T") == 1


def test_exceptions_are_logged_with_traceback_once(tmp_path: Path) -> None:
    handles, stream, _ = start(tmp_path)
    try:
        raise ValueError("bad value")
    except ValueError:
        logging.getLogger("orchestrator.x").exception("failed")
    handles.close()
    text = stream.getvalue()
    assert text.count("Traceback (most recent call last)") == 1
    assert "ValueError: bad value" in text


# --- redaction (LOG-7) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("login with password=hunter2hunter2", "hunter2hunter2"),
        ('{"password": "abc12345"}', "abc12345"),
        ("token: tok-123456789", "tok-123456789"),
        ("Authorization: Bearer abcdef1234567890", "abcdef1234567890"),
        ("retry with Bearer abcdef1234567890", "abcdef1234567890"),
        ("api_key='k-987654321'", "k-987654321"),
        (f"user typed {SECRET}", SECRET),
    ],
)
def test_redactor_masks_secrets(text: str, leaked: str) -> None:
    redact = Redactor([SECRET])
    assert leaked not in redact(text)
    assert "***" in redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "boiler offline since 10:00",
        "environment variable UI_PASSWORD: required when [web] login_required = true",
        "manual on target=4.4 min=4.4",
    ],
)
def test_redactor_leaves_ordinary_text_alone(text: str) -> None:
    assert Redactor([SECRET])(text) == text


def test_short_known_secrets_are_not_replaced_by_value() -> None:
    assert Redactor(["abc"])("abc def") == "abc def"


def test_secrets_can_be_registered_later() -> None:
    redact = Redactor()
    assert redact("code zxqvbnmasd") == "code zxqvbnmasd"
    redact.add("zxqvbnmasd")
    assert "zxqvbnmasd" not in redact("code zxqvbnmasd")


def test_no_secret_reaches_any_handler(tmp_path: Path) -> None:
    handles, stream, log_dir = start(tmp_path)
    log = logging.getLogger("orchestrator.test")
    log.info("known secret as argument: %s", SECRET)
    log.info("unknown secret in a pair: password=%s", UNREGISTERED)
    log.warning("header Authorization: Bearer %s", UNREGISTERED)
    try:
        raise RuntimeError(f"connection failed with {SECRET}")
    except RuntimeError:
        log.exception("boom")
    audit("manual_on", who=SECRET, token=UNREGISTERED)
    ring_text = "\n".join(handles.ring.snapshot())
    handles.close()
    texts = {
        "console": stream.getvalue(),
        "main file": (log_dir / "orchestrator.log").read_text(encoding="utf-8"),
        "audit file": (log_dir / "audit.log").read_text(encoding="utf-8"),
        "ring buffer": ring_text,
    }
    for name, text in texts.items():
        assert text, name
        assert SECRET not in text, name
        assert UNREGISTERED not in text, name


def test_startup_banner_shows_version_and_masks_secrets(tmp_path: Path) -> None:
    handles, stream, _ = start(tmp_path)
    log_startup_banner(logging.getLogger("orchestrator.main"), make_config(tmp_path))
    handles.close()
    text = stream.getvalue()
    assert f"event=startup version={__version__}" in text
    assert "section=secrets" in text
    assert "***" in text
    assert SECRET not in text


# --- handlers: rotation, ring buffer, audit, idempotence -------------------------------------


def test_main_log_rotates_and_stays_within_its_size_limit(tmp_path: Path) -> None:
    handles, _, log_dir = start(tmp_path, file_max_bytes=10_000, file_backup_count=2)
    log = logging.getLogger("orchestrator.bulk")
    for number in range(400):
        log.info("line %03d %s", number, "x" * 80)
    handles.close()
    files = sorted(log_dir.glob("orchestrator.log*"))
    assert 2 <= len(files) <= 3
    assert all(path.stat().st_size <= 10_000 for path in files)


def test_ring_buffer_keeps_the_newest_events_in_order(tmp_path: Path) -> None:
    handles, _, _ = start(tmp_path, ring_size=10)
    log = logging.getLogger("orchestrator.ring")
    for number in range(25):
        log.info("event number %02d", number)
    events = handles.ring.snapshot()
    handles.close()
    assert len(events) == 10
    assert "event number 15" in events[0]
    assert "event number 24" in events[-1]


def test_audit_records_go_only_to_the_audit_log(tmp_path: Path) -> None:
    handles, stream, log_dir = start(tmp_path)
    with correlation("feed1234"):
        audit("manual_on", who="ui:admin", channel="ui", result="ok")
    logging.getLogger("orchestrator.other").info("ordinary line")
    handles.close()
    audit_text = (log_dir / "audit.log").read_text(encoding="utf-8")
    assert "event=manual_on who=ui:admin channel=ui result=ok cid=feed1234" in audit_text
    assert "ordinary line" not in audit_text
    assert "manual_on" not in stream.getvalue()
    assert "manual_on" not in (log_dir / "orchestrator.log").read_text(encoding="utf-8")


def test_setup_is_idempotent(tmp_path: Path) -> None:
    start(tmp_path)
    handles, _, _ = start(tmp_path)
    root_handlers = [
        h for h in logging.getLogger().handlers if (h.name or "").startswith("orchestrator-")
    ]
    assert len(root_handlers) == 3  # console, file, ring buffer
    handles.close()


def test_noisy_libraries_are_limited_to_warnings(tmp_path: Path) -> None:
    handles, _, _ = start(tmp_path)
    handles.close()
    for name in ("urllib3", "paramiko", "waitress", "googleapiclient"):
        assert logging.getLogger(name).level == logging.WARNING


# --- collapsing repeated warnings (LOG-5) ----------------------------------------------------


def _record(level: int, message: str) -> logging.LogRecord:
    return logging.LogRecord("orchestrator.x", level, __file__, 1, message, None, None)


def test_identical_warnings_are_collapsed_and_counted() -> None:
    now = [0.0]

    def clock() -> float:
        return now[0]

    collapse = CollapseFilter(60.0, clock)
    assert collapse.filter(_record(logging.WARNING, "boiler down"))
    for _ in range(4):
        now[0] += 1
        assert not collapse.filter(_record(logging.WARNING, "boiler down"))
    now[0] = 120.0
    again = _record(logging.WARNING, "boiler down")
    assert collapse.filter(again)
    assert "(repeated 4 more times in the last 2 min)" in str(again.msg)


def test_info_and_different_messages_are_never_collapsed() -> None:
    collapse = CollapseFilter(60.0, lambda: 0.0)
    assert all(collapse.filter(_record(logging.INFO, "same")) for _ in range(3))
    assert collapse.filter(_record(logging.WARNING, "one"))
    assert collapse.filter(_record(logging.WARNING, "two"))


# --- run-time level change (LOG-10) ----------------------------------------------------------


def test_debug_window_switches_to_debug_and_back() -> None:
    logging.getLogger().setLevel(logging.INFO)
    window = DebugWindow("INFO", duration_s=0.05)
    assert window.toggle() is True
    assert logging.getLogger().level == logging.DEBUG
    deadline = time.monotonic() + 3.0
    while window.active and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not window.active
    assert logging.getLogger().level == logging.INFO


def test_debug_window_can_be_switched_off_early() -> None:
    logging.getLogger().setLevel(logging.INFO)
    window = DebugWindow("INFO", duration_s=60.0)
    assert window.toggle() is True
    assert window.toggle() is False
    assert logging.getLogger().level == logging.INFO
