#!/usr/bin/env python3
"""Validate an air780e-hub deployment without third-party dependencies."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


USER_AGENT = "air780e-hub-self-check/0.1"
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HTTP_HEADER = 64 * 1024
MAX_FRAME = 1024 * 1024


class CheckError(RuntimeError):
    pass


def normalize_base_url(raw: str, *, allow_http: bool) -> str:
    value = raw.strip().rstrip("/")
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        raise CheckError("--url must use https:// (or http:// with --allow-http)")
    if not parts.hostname:
        raise CheckError("--url must include a host")
    if parts.username or parts.password:
        raise CheckError("credentials must not be embedded in --url")
    if parts.query or parts.fragment:
        raise CheckError("--url must not contain a query string or fragment")
    if parts.scheme == "http" and not allow_http:
        raise CheckError("plain HTTP is disabled; use HTTPS in production")
    try:
        _ = parts.port
    except ValueError as exc:
        raise CheckError(f"invalid port in --url: {exc}") from exc
    return value


def validate_environment(environ: Mapping[str, str], *, using_https: bool) -> list[str]:
    """Validate Compose-facing settings and return non-fatal warnings."""
    warnings: list[str] = []

    bind = environ.get("HUB_BIND_ADDRESS", "127.0.0.1").strip()
    if not bind:
        raise CheckError("HUB_BIND_ADDRESS must not be empty")
    if bind in {"0.0.0.0", "::"}:
        warnings.append(
            "HUB_BIND_ADDRESS exposes plain HTTP on all interfaces; verify firewalling"
        )

    _bounded_int(environ, "HUB_HOST_PORT", default=8090, minimum=1, maximum=65535)
    _bounded_int(environ, "HUB_MESSAGE_RETENTION_DAYS", default=90, minimum=0)
    _bounded_int(environ, "HUB_STATUS_RETENTION_DAYS", default=30, minimum=0)

    behind_proxy = _boolean(environ, "HUB_BEHIND_PROXY", default=True)
    if using_https and not behind_proxy:
        raise CheckError(
            "HUB_BEHIND_PROXY must be true when TLS terminates at a reverse proxy"
        )

    timezone = environ.get("HUB_TZ", "Asia/Shanghai").strip()
    if not timezone:
        raise CheckError("HUB_TZ must not be empty")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CheckError(f"HUB_TZ is not a known timezone: {timezone}") from exc

    return warnings


def _bounded_int(
    environ: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    raw = environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise CheckError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        limit = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise CheckError(f"{name} must be {limit}")
    return value


def _boolean(environ: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise CheckError(f"{name} must be true or false")


def read_token(args: argparse.Namespace, environ: Mapping[str, str]) -> str:
    if args.token_file:
        path = Path(args.token_file)
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CheckError(f"cannot read token file {path}: {exc}") from exc
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise CheckError(f"token file {path} must not be accessible by group or others")
    else:
        token = environ.get(args.token_env, "").strip()
        if not token:
            raise CheckError(
                f"set {args.token_env} or pass --token-file; the token is never printed"
            )
    if "\r" in token or "\n" in token:
        raise CheckError("agent token contains a newline")
    return token


def check_health(base_url: str, *, timeout: float) -> dict[str, object]:
    url = f"{base_url}/healthz"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise CheckError(f"health endpoint returned HTTP {response.status}")
            raw = response.read(MAX_FRAME + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise CheckError(f"health endpoint is unreachable: {exc}") from exc
    if len(raw) > MAX_FRAME:
        raise CheckError("health response is unexpectedly large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckError("health endpoint did not return valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise CheckError("health response does not contain ok=true")
    return payload


def check_websocket(base_url: str, token: str, *, timeout: float) -> None:
    parts = urllib.parse.urlsplit(base_url)
    secure = parts.scheme == "https"
    ws_scheme = "wss" if secure else "ws"
    ws_url = urllib.parse.urlunsplit(
        (ws_scheme, parts.netloc, f"{parts.path}/ws", "self_check=1", "")
    )
    _websocket_self_check(ws_url, token, timeout=timeout)


def _websocket_self_check(url: str, token: str, *, timeout: float) -> None:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if host is None:
        raise CheckError("WebSocket URL has no host")
    secure = parts.scheme == "wss"
    port = parts.port or (443 if secure else 80)

    try:
        raw_socket = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise CheckError(f"WebSocket check failed: {exc}") from exc
    connection: socket.socket = raw_socket
    try:
        if secure:
            context = ssl.create_default_context()
            connection = context.wrap_socket(raw_socket, server_hostname=host)
            connection.settimeout(timeout)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        target = parts.path or "/"
        if parts.query:
            target += f"?{parts.query}"
        request = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {parts.netloc}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {token}\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
            "\r\n"
        ).encode("ascii")
        connection.sendall(request)

        status, headers, buffered = _read_upgrade_response(connection)
        if status != 101:
            raise CheckError(f"WebSocket upgrade returned HTTP {status}")
        if headers.get("upgrade", "").lower() != "websocket":
            raise CheckError("WebSocket upgrade response has no Upgrade header")
        connection_tokens = {
            item.strip().lower() for item in headers.get("connection", "").split(",")
        }
        if "upgrade" not in connection_tokens:
            raise CheckError("WebSocket upgrade response has no Connection: Upgrade")
        expected = base64.b64encode(
            hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise CheckError("WebSocket Sec-WebSocket-Accept is invalid")

        opcode, payload = _read_frame(connection, buffered)
        if opcode == 0x8:
            code, reason = _decode_close(payload)
            raise CheckError(f"WebSocket closed with code {code}: {reason}")
        if opcode != 0x1:
            raise CheckError(f"expected a text self-check frame, got opcode {opcode}")
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CheckError("WebSocket self-check response is not valid JSON") from exc
        if message != {"type": "self_check", "ok": True}:
            raise CheckError("WebSocket self-check response is invalid")

        try:
            _send_frame(connection, 0x8, struct.pack("!H", 1000))
        except OSError:
            pass
    except (OSError, ssl.SSLError) as exc:
        raise CheckError(f"WebSocket check failed: {exc}") from exc
    finally:
        connection.close()


def _read_upgrade_response(
    connection: socket.socket,
) -> tuple[int, dict[str, str], bytearray]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(4096)
        if not chunk:
            raise CheckError("connection closed during WebSocket upgrade")
        data.extend(chunk)
        if len(data) > MAX_HTTP_HEADER:
            raise CheckError("WebSocket upgrade headers are too large")

    raw_headers, remainder = bytes(data).split(b"\r\n\r\n", 1)
    try:
        lines = raw_headers.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
    except (UnicodeDecodeError, ValueError, IndexError) as exc:
        raise CheckError("invalid HTTP response during WebSocket upgrade") from exc

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            raise CheckError("malformed WebSocket upgrade header")
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return status, headers, bytearray(remainder)


def _read_frame(
    connection: socket.socket, buffered: bytearray
) -> tuple[int, bytes]:
    first, second = _read_exact(connection, buffered, 2)
    if first & 0x70:
        raise CheckError("WebSocket response uses unsupported RSV bits")
    if not first & 0x80:
        raise CheckError("fragmented WebSocket self-check response is unsupported")
    if second & 0x80:
        raise CheckError("server WebSocket frames must not be masked")

    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(connection, buffered, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(connection, buffered, 8))[0]
    if length > MAX_FRAME:
        raise CheckError("WebSocket self-check frame is too large")
    return first & 0x0F, _read_exact(connection, buffered, length)


def _read_exact(connection: socket.socket, buffered: bytearray, size: int) -> bytes:
    while len(buffered) < size:
        chunk = connection.recv(max(4096, size - len(buffered)))
        if not chunk:
            raise CheckError("connection closed while reading a WebSocket frame")
        buffered.extend(chunk)
    result = bytes(buffered[:size])
    del buffered[:size]
    return result


def _send_frame(connection: socket.socket, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = bytes((0x80 | opcode, 0x80 | length))
    elif length <= 0xFFFF:
        header = bytes((0x80 | opcode, 0xFE)) + struct.pack("!H", length)
    else:
        header = bytes((0x80 | opcode, 0xFF)) + struct.pack("!Q", length)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    connection.sendall(header + mask + masked)


def _decode_close(payload: bytes) -> tuple[int, str]:
    if len(payload) < 2:
        return 1005, "no reason"
    code = struct.unpack("!H", payload[:2])[0]
    return code, payload[2:].decode("utf-8", errors="replace") or "no reason"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="check deployment settings, /healthz, WebSocket proxying and token"
    )
    parser.add_argument("--url", required=True, help="public base URL, normally https://...")
    token = parser.add_mutually_exclusive_group()
    token.add_argument("--token-file", help="0600 file containing the Agent Token")
    token.add_argument(
        "--token-env",
        default="HUB_AGENT_TOKEN",
        help="environment variable containing the Agent Token (default: HUB_AGENT_TOKEN)",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--allow-http",
        action="store_true",
        help="allow plain HTTP for local-only checks",
    )
    return parser


def _report(line: str, *, failure: bool = False) -> None:
    """Emit one result line, keeping stdout and stderr in true order.

    Failures stay on stderr so a wrapper can filter them, but stdout is block
    buffered when piped while stderr is not — without flushing first, a piped
    run shows every [FAIL] ahead of the [PASS] lines that came before it.
    """
    sys.stdout.flush()
    stream = sys.stderr if failure else sys.stdout
    print(line, file=stream)
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout <= 0:
        _report("[FAIL] --timeout must be greater than zero", failure=True)
        return 2

    try:
        base_url = normalize_base_url(args.url, allow_http=args.allow_http)
        token = read_token(args, os.environ)
        warnings = validate_environment(
            os.environ, using_https=base_url.startswith("https://")
        )
    except CheckError as exc:
        _report(f"[FAIL] configuration: {exc}", failure=True)
        return 2

    _report("[PASS] deployment environment")
    for warning in warnings:
        _report(f"[WARN] {warning}")

    failures = 0
    try:
        health = check_health(base_url, timeout=args.timeout)
        _report(
            "[PASS] health endpoint "
            f"(agents_connected={health.get('agents_connected', 'unknown')})"
        )
    except CheckError as exc:
        failures += 1
        _report(f"[FAIL] health endpoint: {exc}", failure=True)

    try:
        check_websocket(base_url, token, timeout=args.timeout)
        _report("[PASS] WebSocket upgrade, proxy headers and Agent Token")
    except CheckError as exc:
        failures += 1
        _report(f"[FAIL] WebSocket: {exc}", failure=True)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
