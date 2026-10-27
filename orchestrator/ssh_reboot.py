"""Reboot the boiler Pi over SSH (FR-LEG-3a, CS-15).

Rules, in this order:

1. the host key must be configured (``[boiler] ssh_host_key``); without it nothing is contacted
   beyond the key exchange, because credentials are never given to a host that was not recognised;
2. the key exchange runs and the host key's SHA-256 fingerprint must equal the configured one;
3. only then the user logs in, preferably with a key file, otherwise with the password
   (both come from environment variables);
4. exactly one fixed command runs: ``sudo reboot``. No text from a user ever reaches a shell.

A connection that drops while the command runs is the expected result of a reboot and counts as
success. Connecting and running have time limits. Messages never contain a password, key, or the
text of a library error.
"""

import argparse
import base64
import hashlib
import hmac
import importlib
import logging
import socket
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from orchestrator.config import ConfigError, load_config
from orchestrator.logging_setup import audit, kv

logger = logging.getLogger(__name__)

REBOOT_COMMAND = "sudo reboot"


class RebootError(Exception):
    """The reboot did not happen; the text is safe to show and to e-mail."""


@dataclass(frozen=True, slots=True)
class SshTarget:
    host: str
    port: int
    user: str | None
    key_file: str | None
    password: str | None
    host_key: str  # expected "SHA256:..." fingerprint; empty means "not configured"
    timeout_s: float


class SshSession(Protocol):
    """One SSH connection, in the order in which it must be used."""

    def host_key_fingerprint(self) -> str: ...

    def authenticate(self, user: str, key_file: str | None, password: str | None) -> None: ...

    def run(self, command: str, timeout_s: float) -> int | None:
        """Run a command; the exit status, or ``None`` if the connection ended without one."""
        ...

    def close(self) -> None: ...


Connector = Callable[[str, int, float], SshSession]


def fingerprint_of(key_blob: bytes) -> str:
    """The OpenSSH style fingerprint of a public key blob."""
    digest = hashlib.sha256(key_blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class ParamikoSession:
    """The real SSH connection (paramiko is imported only when one is made)."""

    def __init__(self, host: str, port: int, timeout_s: float) -> None:
        self._paramiko = importlib.import_module("paramiko")
        self._sock = socket.create_connection((host, port), timeout=timeout_s)
        try:
            self._transport = self._paramiko.Transport(self._sock)
            self._transport.start_client(timeout=timeout_s)
        except BaseException:
            self.close()
            raise

    def host_key_fingerprint(self) -> str:
        return fingerprint_of(self._transport.get_remote_server_key().asbytes())

    def authenticate(self, user: str, key_file: str | None, password: str | None) -> None:
        if key_file:
            key = self._paramiko.PKey.from_path(key_file)
            self._transport.auth_publickey(user, key)
        elif password:
            self._transport.auth_password(user, password)
        else:
            raise RebootError("no SSH key file or password is set")

    def run(self, command: str, timeout_s: float) -> int | None:
        channel = self._transport.open_session(timeout=timeout_s)
        channel.settimeout(timeout_s)
        channel.exec_command(command)  # a failure here means the command was not accepted
        try:
            finished = channel.status_event.wait(timeout_s)  # no busy waiting, but a time limit
        except (EOFError, OSError, self._paramiko.SSHException):
            return None  # the connection broke after the command was accepted: the Pi is going down
        if not finished:
            raise RebootError("the reboot command did not finish in time")
        status: int = channel.exit_status
        return None if status < 0 else status  # -1: closed without a status, as a reboot does

    def close(self) -> None:
        # a transport that never finished starting does not close its socket, so do it here
        transport = getattr(self, "_transport", None)
        if transport is not None:
            transport.close()
        self._sock.close()


def connect_with_paramiko(host: str, port: int, timeout_s: float) -> SshSession:
    return ParamikoSession(host, port, timeout_s)


class SshReboot:
    """Implements the ``Rebooter`` interface of the e-mail commands."""

    def __init__(
        self,
        target: SshTarget,
        *,
        connect: Connector = connect_with_paramiko,
        on_requested: Callable[[], None] | None = None,
    ) -> None:
        self._target = target
        self._connect = connect
        self._on_requested = on_requested

    def reboot(self) -> str:
        target = self._target
        if not target.host_key:
            raise RebootError("the boiler Pi's host key is not configured ([boiler] ssh_host_key)")
        if not target.user:
            raise RebootError("the SSH user is not set (BOILER_SSH_USER)")
        if not (target.key_file or target.password):
            raise RebootError("neither BOILER_SSH_KEY_FILE nor BOILER_SSH_PASSWORD is set")
        try:
            session = self._connect(target.host, target.port, target.timeout_s)
        except OSError:
            raise RebootError("the boiler Pi could not be reached over SSH") from None
        except Exception as error:
            raise RebootError(f"the SSH connection failed ({type(error).__name__})") from None
        try:
            self._check_host_key(session)
            self._log_in(session)
            status = self._run(session)
        finally:
            self._close(session)
        if status not in (None, 0):
            raise RebootError(f"the reboot command failed (exit status {status})")
        audit("boiler_reboot", host=target.host, result="requested")
        logger.warning(kv("boiler_reboot_requested", host=target.host))
        if self._on_requested is not None:
            self._on_requested()
        return "reboot requested; the boiler Pi will be unreachable for a few minutes"

    def _check_host_key(self, session: SshSession) -> None:
        try:
            seen = session.host_key_fingerprint()
        except Exception as error:
            raise RebootError(f"no host key was received ({type(error).__name__})") from None
        if not hmac.compare_digest(seen.encode(), self._target.host_key.encode()):
            logger.error(kv("ssh_host_key_mismatch", host=self._target.host))
            audit("boiler_reboot", host=self._target.host, result="host_key_mismatch")
            raise RebootError(
                "the host key of the boiler Pi is not the configured one; nothing was sent"
            )

    def _log_in(self, session: SshSession) -> None:
        try:
            session.authenticate(
                self._target.user or "", self._target.key_file, self._target.password
            )
        except RebootError:
            raise
        except Exception as error:
            raise RebootError(f"the SSH login was refused ({type(error).__name__})") from None

    def _run(self, session: SshSession) -> int | None:
        try:
            return session.run(REBOOT_COMMAND, self._target.timeout_s)
        except RebootError:
            raise
        except OSError:
            return None  # the connection dropped while the Pi went down: that is the success case
        except Exception as error:
            raise RebootError(
                f"the reboot command could not be sent ({type(error).__name__})"
            ) from None

    @staticmethod
    def _close(session: SshSession) -> None:
        try:
            session.close()
        except Exception:  # the connection is usually gone already after a reboot
            logger.debug(kv("ssh_close_failed"))


# --- showing the host key, to be compared by a person and copied into the configuration --------


def fetch_host_key(host: str, port: int, timeout_s: float, connect: Connector) -> str:
    session = connect(host, port, timeout_s)
    try:
        return session.host_key_fingerprint()
    finally:
        session.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    connect: Connector = connect_with_paramiko,
    out: Any = None,
) -> int:
    stream = sys.stdout if out is None else out
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.ssh_reboot",
        description="Show the SSH host key fingerprint of the boiler Pi (nothing is sent).",
    )
    parser.add_argument("--show-host-key", action="store_true", required=True)
    parser.add_argument("--config", help="configuration file (default: built-in defaults)")
    args = parser.parse_args(argv)
    try:
        config = load_config(Path(args.config) if args.config else None, env={})
    except ConfigError as error:
        print(f"error: {error}", file=stream)
        return 2
    host = urlsplit(config.boiler.base_url).hostname or ""
    try:
        found = fetch_host_key(host, config.boiler.ssh_port, config.boiler.ssh_timeout_s, connect)
    except Exception as error:
        print(f"error: could not read the host key of {host} ({type(error).__name__})", file=stream)
        return 1
    print(f"Host key of {host}:{config.boiler.ssh_port}", file=stream)
    print(f"  {found}", file=stream)
    print(
        "Compare it with the output of 'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub' "
        "(or the ecdsa / rsa file) run on the boiler Pi itself, then put it in [boiler] "
        "ssh_host_key.",
        file=stream,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
