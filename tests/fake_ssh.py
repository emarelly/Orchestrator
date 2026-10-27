"""A small real SSH server on 127.0.0.1 for tests, built with paramiko's server side.

It lets the code under test make a genuine SSH connection (key exchange, authentication, command)
without any real machine. It records every authentication attempt and every command, so a test can
prove that nothing was sent to a host whose key was not the expected one.
"""

import importlib
import socket
import threading
import time
from pathlib import Path
from typing import Any

paramiko: Any = importlib.import_module("paramiko")

AUTH_OK = paramiko.AUTH_SUCCESSFUL
AUTH_FAILED = paramiko.AUTH_FAILED


class FakeSshServer:
    """Accepts connections until stopped.

    ``exit_status``: send this status for a command and close; ``None`` closes the channel without a
    status (what a reboot looks like); ``hang`` never finishes the command.
    """

    def __init__(
        self,
        *,
        user: str = "pi",
        password: str | None = "right-password",
        accepted_key: Any = None,
        exit_status: int | None = None,
        hang: bool = False,
    ) -> None:
        self.host_key = paramiko.ECDSAKey.generate()
        self.user = user
        self.password = password
        self.accepted_key = accepted_key
        self.exit_status = exit_status
        self.hang = hang
        self.logins: list[str] = []  # "password" or "key", one per attempt
        self.commands: list[str] = []
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port: int = self._listener.getsockname()[1]
        self._transports: list[Any] = []
        self._connections: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._stopping = False

    def start(self) -> "FakeSshServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopping = True
        self._listener.close()
        self._thread.join(timeout=5)
        for transport in self._transports:
            transport.close()
        for conn in self._connections:
            conn.close()

    def __enter__(self) -> "FakeSshServer":
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()

    def _accept_loop(self) -> None:
        while not self._stopping:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            self._connections.append(conn)
            transport = paramiko.Transport(conn)
            transport.add_server_key(self.host_key)
            self._transports.append(transport)
            try:
                transport.start_server(server=self._interface())
            except Exception:
                transport.close()
                conn.close()  # a transport that never started leaves its socket open

    def _interface(self) -> Any:
        outer = self

        def get_allowed_auths(_self: Any, username: str) -> str:
            return "password,publickey"

        def check_auth_password(_self: Any, username: str, password: str) -> int:
            outer.logins.append("password")
            good = username == outer.user and outer.password is not None
            return int(AUTH_OK if good and password == outer.password else AUTH_FAILED)

        def check_auth_publickey(_self: Any, username: str, key: Any) -> int:
            outer.logins.append("key")
            wanted = outer.accepted_key
            good = wanted is not None and username == outer.user
            return int(AUTH_OK if good and key.asbytes() == wanted.asbytes() else AUTH_FAILED)

        def check_channel_request(_self: Any, kind: str, chanid: int) -> int:
            refused = paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            return int(paramiko.OPEN_SUCCEEDED if kind == "session" else refused)

        def check_channel_exec_request(_self: Any, channel: Any, command: bytes) -> bool:
            outer.commands.append(command.decode())
            threading.Thread(target=outer._finish, args=(channel,), daemon=True).start()
            return True

        methods = {
            "get_allowed_auths": get_allowed_auths,
            "check_auth_password": check_auth_password,
            "check_auth_publickey": check_auth_publickey,
            "check_channel_request": check_channel_request,
            "check_channel_exec_request": check_channel_exec_request,
        }
        return type("FakeInterface", (paramiko.ServerInterface,), methods)()

    def _finish(self, channel: Any) -> None:
        time.sleep(0.1)  # a real sshd has answered the request before the command ends
        if self.hang:
            return
        try:
            if self.exit_status is not None:
                channel.send_exit_status(self.exit_status)
            channel.close()
        except (EOFError, OSError, paramiko.SSHException):
            pass  # the client or the test already tore the connection down; nothing is left to do


def write_private_key(key: Any, path: Path) -> str:
    key.write_private_key_file(str(path))
    return str(path)
