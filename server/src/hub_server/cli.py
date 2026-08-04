"""Server entry point and offline recovery commands."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from .auth import Auth, AuthError
from .config import Settings
from .db import Database

log = logging.getLogger("hub-server")


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .main import create_app

    settings = Settings.from_env()
    if args.port:
        settings.port = args.port

    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        # Trust forwarding headers only when deployment enables proxy mode.
        proxy_headers=settings.behind_proxy,
        forwarded_allow_ips="*" if settings.behind_proxy else None,
        log_level="debug" if args.verbose else "info",
    )
    return 0


def _auth(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    db = Database(settings.db_path)
    auth = Auth(db, session_ttl_hours=settings.session_ttl_hours)
    try:
        if args.auth_command == "reset-password":
            password = getpass.getpass("new administrator password: ")
            confirm = getpass.getpass("confirm: ")
            if password != confirm:
                print("passwords do not match", file=sys.stderr)
                return 1
            try:
                auth.set_password(password)
            except AuthError as exc:
                print(f"rejected: {exc}", file=sys.stderr)
                return 1
            print("password set; all existing sessions were signed out")
        elif args.auth_command == "clear":
            auth.clear()
            print("password cleared; the web UI will ask to set one on next visit")
        elif args.auth_command == "status":
            print(f"configured: {auth.is_configured}")
        return 0
    finally:
        db.close()


def _token(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    print(settings.agent_token)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hub-server")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the server (default)")
    serve.add_argument("--port", type=int)

    auth = sub.add_parser("auth", help="administrator password recovery")
    auth.add_argument(
        "auth_command", choices=["reset-password", "clear", "status"]
    )

    sub.add_parser("token", help="print the agent token")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    if args.command == "auth":
        return _auth(args)
    if args.command == "token":
        return _token(args)
    return _serve(args)


if __name__ == "__main__":
    sys.exit(main())
