"""Command-line tool for the Google authorization: ``python -m orchestrator.auth`` (FR-GOOG-3).

Without options it runs the consent flow. ``--check`` only reports the state of the token,
``--import-oauth2client FILE`` converts a token written by the old library. Paths come from the
options or from ``GOOGLE_CLIENT_SECRET_FILE`` / ``GOOGLE_TOKEN_FILE``.
"""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from orchestrator.google_auth import (
    PURPOSES,
    AuthError,
    FlowFactory,
    TokenState,
    TokenStatus,
    authorize,
    import_old_token,
    refresh_with_google,
    status_for,
)

_REAUTHORIZE = (TokenState.REVOKED, TokenState.SCOPES_MISSING, TokenState.MISSING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.auth", description="Authorize access to Google."
    )
    parser.add_argument("--client-secret", type=Path, help="client secret file from Google Cloud")
    parser.add_argument("--token", type=Path, help="where the token file is stored")
    parser.add_argument("--port", type=int, default=8080, help="local port for the consent flow")
    parser.add_argument(
        "--no-browser", action="store_true", help="print the address instead of opening a browser"
    )
    parser.add_argument("--check", action="store_true", help="report the token state and exit")
    parser.add_argument(
        "--offline", action="store_true", help="with --check: do not contact Google"
    )
    parser.add_argument(
        "--import-oauth2client", type=Path, metavar="FILE", help="convert an old token file"
    )
    parser.add_argument(
        "--for",
        dest="purpose",
        choices=PURPOSES,
        default="both",
        help="what the token is for when checking or importing (default: both)",
    )
    return parser


def _report(status: TokenStatus, out: TextIO) -> int:
    print(f"token: {status.state.value} - {status.detail}", file=out)
    for scope in status.missing_scopes:
        print(f"  missing permission: {scope}", file=out)
    if status.state in _REAUTHORIZE:
        print("Run 'python -m orchestrator.auth' to authorize again.", file=out)
    return 0 if status.usable else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    flow_factory: FlowFactory | None = None,
) -> int:
    """Run the tool; returns the exit code (0 = token usable)."""
    environment = os.environ if env is None else env
    stream = sys.stdout if out is None else out
    args = build_parser().parse_args(argv)
    token = args.token or _path(environment.get("GOOGLE_TOKEN_FILE"))
    if token is None:
        print("error: give --token or set GOOGLE_TOKEN_FILE", file=stream)
        return 2
    try:
        if args.import_oauth2client is not None:
            return _report(import_old_token(args.import_oauth2client, token, args.purpose), stream)
        if args.check:
            refresh = None if args.offline else refresh_with_google
            return _report(status_for(token, args.purpose, refresh=refresh), stream)
        client_secret = args.client_secret or _path(environment.get("GOOGLE_CLIENT_SECRET_FILE"))
        if client_secret is None:
            print("error: give --client-secret or set GOOGLE_CLIENT_SECRET_FILE", file=stream)
            return 2
        extra = {} if flow_factory is None else {"flow_factory": flow_factory}
        status = authorize(
            client_secret, token, port=args.port, open_browser=not args.no_browser, **extra
        )
    except AuthError as error:
        print(f"error: {error}", file=stream)
        return 1
    return _report(status, stream)


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None


if __name__ == "__main__":
    raise SystemExit(main())
