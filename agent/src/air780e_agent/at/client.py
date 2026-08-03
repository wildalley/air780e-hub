"""Serialized AT command client with URC dispatch.

AT is half-duplex: one command may be outstanding at a time on a given port,
and its response is terminated by a *final result code*.  What makes a naive
implementation fail in the field is that unsolicited result codes (URCs) —
``+CMTI`` for a new message, ``+CREG`` for a registration change — can land in
the middle of another command's response.

The rule used here: a line is a URC if and only if its prefix was explicitly
registered.  Everything else belongs to the command in flight.  That keeps
``+CMGL:`` (a response) from being mistaken for an unsolicited report while
still letting ``+CMTI:`` cut the queue.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .errors import (
    ATCommandError,
    ATError,
    ATTimeout,
    CmeError,
    CmsError,
    TransportClosed,
)
from .transport import Transport

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
PROMPT_TIMEOUT = 10.0

_FINAL_OK = "OK"
_FINAL_PLAIN_ERRORS = {
    "ERROR",
    "NO CARRIER",
    "NO DIALTONE",
    "NO ANSWER",
    "BUSY",
    "ABORTED",
    "COMMAND NOT SUPPORT",
}
_CME_RE = re.compile(r"^\+CME ERROR:\s*(\d+)", re.I)
_CMS_RE = re.compile(r"^\+CMS ERROR:\s*(\d+)", re.I)
_CME_TEXT_RE = re.compile(r"^\+(CME|CMS) ERROR:\s*(.+)$", re.I)


@dataclass
class ATResponse:
    """Informational lines plus the final result code of one command."""

    command: str
    lines: list[str] = field(default_factory=list)
    final: str = _FINAL_OK

    def first(self, prefix: str) -> str | None:
        """Value of the first line starting with ``prefix`` (prefix stripped)."""
        for line in self.lines:
            if line.upper().startswith(prefix.upper()):
                return line[len(prefix) :].strip()
        return None

    def all(self, prefix: str) -> list[str]:
        return [
            line[len(prefix) :].strip()
            for line in self.lines
            if line.upper().startswith(prefix.upper())
        ]


@dataclass
class ATUrc:
    name: str  # e.g. "+CMTI"
    params: str  # everything after the colon
    payload: list[str] = field(default_factory=list)


UrcHandler = Callable[[ATUrc], None | Awaitable[None]]


@dataclass
class _Registration:
    prefix: str
    handler: UrcHandler
    payload_lines: int = 0


@dataclass
class _Pending:
    command: str
    future: asyncio.Future
    lines: list[str] = field(default_factory=list)
    prompt: asyncio.Event = field(default_factory=asyncio.Event)
    # Response prefix this command owns, e.g. "+CEREG" for "AT+CEREG?".
    expected_prefix: str | None = None


_COMMAND_PREFIX_RE = re.compile(r"^AT(\+[A-Z0-9]+)", re.I)


def expected_response_prefix(command: str) -> str | None:
    """The ``+XXX`` a command's own response will carry, if any.

    ``AT+CEREG?`` answers ``+CEREG: 0,1`` — the very same prefix that arrives
    unsolicited when registration changes.  Knowing which command is in
    flight is what tells the two apart.
    """
    match = _COMMAND_PREFIX_RE.match(command.strip())
    return match.group(1).upper() if match else None


class ATClient:
    def __init__(
        self,
        transport: Transport,
        *,
        name: str = "modem",
        default_timeout: float = DEFAULT_TIMEOUT,
        trace: Callable[[str, str], None] | None = None,
    ) -> None:
        self.transport = transport
        self.name = name
        self.default_timeout = default_timeout
        # trace(direction, text) — feeds the web AT console and debug logs.
        self._trace = trace

        self._lock = asyncio.Lock()
        self._pending: _Pending | None = None
        self._registrations: list[_Registration] = []
        self._buffer = bytearray()
        self._urc_capture: tuple[_Registration, ATUrc] | None = None
        self._closed = False

        transport.set_reader(self._feed)

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> None:
        self._closed = False
        await self.transport.open()

    async def close(self) -> None:
        self._closed = True
        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.future.set_exception(TransportClosed(f"{self.name} closed"))
        await self.transport.close()

    # -- URC registration --------------------------------------------------

    def register_urc(
        self, prefix: str, handler: UrcHandler, *, payload_lines: int = 0
    ) -> None:
        """Route lines beginning with ``prefix`` to ``handler``.

        ``payload_lines`` captures that many following lines into the URC —
        ``+CMT`` for instance is a header line followed by the raw PDU.
        """
        self._registrations.append(
            _Registration(prefix.upper(), handler, payload_lines)
        )

    def _match_urc(self, line: str) -> _Registration | None:
        upper = line.upper()
        for reg in self._registrations:
            if not upper.startswith(reg.prefix):
                continue
            # Require a boundary so "+CMT" does not swallow "+CMTI:".
            rest = upper[len(reg.prefix) :]
            if rest == "" or rest[0] in ": ,":
                return reg
        return None

    # -- reading -----------------------------------------------------------

    def _feed(self, data: bytes) -> None:
        if not data:
            return
        self._buffer.extend(data)
        if self._trace is not None:
            self._trace("rx", data.decode("utf-8", errors="replace"))

        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                break
            raw = self._buffer[: index + 1]
            del self._buffer[: index + 1]
            line = raw.decode("utf-8", errors="replace").strip("\r\n")
            if line:
                self._handle_line(line)

        # The "> " send prompt arrives without a line terminator, so it can
        # only be spotted by looking at what is still sitting in the buffer.
        pending = self._pending
        if pending is not None and not pending.prompt.is_set():
            tail = bytes(self._buffer)
            if tail.rstrip(b" ").endswith(b">") or tail.endswith(b"> "):
                self._buffer.clear()
                pending.prompt.set()

    def _handle_line(self, line: str) -> None:
        # Continuation lines of a multi-line URC take priority.
        if self._urc_capture is not None:
            reg, urc = self._urc_capture
            urc.payload.append(line)
            if len(urc.payload) >= reg.payload_lines:
                self._urc_capture = None
                self._dispatch_urc(reg, urc)
            return

        pending = self._pending

        # A command owns its own response prefix even when that prefix is also
        # registered as a URC — "+CEREG: 0,1" answers AT+CEREG? here, rather
        # than being reported as a registration change.
        if (
            pending is not None
            and pending.expected_prefix is not None
            and line.upper().startswith(pending.expected_prefix + ":")
        ):
            pending.lines.append(line)
            return

        reg = self._match_urc(line)
        if reg is not None:
            name, _, params = line.partition(":")
            urc = ATUrc(name=name.strip(), params=params.strip())
            if reg.payload_lines > 0:
                self._urc_capture = (reg, urc)
            else:
                self._dispatch_urc(reg, urc)
            return

        if pending is None:
            log.debug("[%s] unsolicited line with no handler: %s", self.name, line)
            return

        if line == pending.command:  # echo, if ATE0 has not taken effect
            return

        if self._is_final(line, pending):
            return

        pending.lines.append(line)

    def _is_final(self, line: str, pending: _Pending) -> bool:
        upper = line.upper()
        error: ATError | None = None

        if upper == _FINAL_OK:
            pass
        elif match := _CME_RE.match(line):
            error = CmeError(int(match.group(1)), command=pending.command)
        elif match := _CMS_RE.match(line):
            error = CmsError(int(match.group(1)), command=pending.command)
        elif match := _CME_TEXT_RE.match(line):
            # +CMEE=1 gives numeric codes, =2 gives text; accept both.
            error = ATCommandError(match.group(0), command=pending.command)
        elif upper in _FINAL_PLAIN_ERRORS:
            error = ATCommandError(line, command=pending.command)
        else:
            return False

        if not pending.future.done():
            if error is not None:
                pending.future.set_exception(error)
            else:
                pending.future.set_result(
                    ATResponse(pending.command, list(pending.lines), line)
                )
        return True

    def _dispatch_urc(self, reg: _Registration, urc: ATUrc) -> None:
        try:
            result = reg.handler(urc)
            if asyncio.iscoroutine(result):
                asyncio.get_running_loop().create_task(result)
        except Exception:
            log.exception("[%s] URC handler failed for %s", self.name, urc.name)

    # -- writing -----------------------------------------------------------

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        payload: bytes | str | None = None,
        expect_prompt: bool = False,
        prompt_timeout: float = PROMPT_TIMEOUT,
    ) -> ATResponse:
        """Run one command to its final result code.

        ``expect_prompt`` handles the two-step commands (``AT+CMGS``,
        ``AT+CMGW``) that answer ``> `` and then wait for a Ctrl-Z terminated
        body before producing a result.
        """
        if self._closed:
            raise TransportClosed(f"{self.name} is closed")

        timeout = self.default_timeout if timeout is None else timeout

        async with self._lock:
            loop = asyncio.get_running_loop()
            pending = _Pending(
                command=command,
                future=loop.create_future(),
                expected_prefix=expected_response_prefix(command),
            )
            self._pending = pending
            try:
                self._write(command + "\r")

                if expect_prompt:
                    try:
                        await asyncio.wait_for(
                            pending.prompt.wait(), timeout=prompt_timeout
                        )
                    except asyncio.TimeoutError:
                        raise ATTimeout(
                            "modem never sent the '>' prompt", command=command
                        ) from None
                    if payload is not None:
                        self._write(payload)

                try:
                    return await asyncio.wait_for(pending.future, timeout=timeout)
                except asyncio.TimeoutError:
                    raise ATTimeout(
                        f"no final result code within {timeout}s", command=command
                    ) from None
            finally:
                self._pending = None
                self._urc_capture = None

    def _write(self, data: bytes | str) -> None:
        raw = data.encode() if isinstance(data, str) else data
        if self._trace is not None:
            self._trace("tx", raw.decode("utf-8", errors="replace"))
        self.transport.write(raw)
