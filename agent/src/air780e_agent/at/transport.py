"""Byte transports for the AT client.

Only two are needed: a real serial port, and an in-process pair used by the
tests and by the mock modem.  Both push received bytes into a callback and
accept writes synchronously; framing is the client's job, not the
transport's.
"""

from __future__ import annotations

import asyncio
import os
import tty
from dataclasses import dataclass
from typing import Callable, Protocol

from .errors import TransportClosed

ReaderCallback = Callable[[bytes], None]
# Called once when the transport dies under us — an unplugged module, a USB
# reset.  Nothing else notices on its own: an fd that has gone away simply
# stops producing bytes, and a reader callback has no caller to raise to.
CloseCallback = Callable[[Exception | None], None]


class Transport(Protocol):
    def set_reader(self, callback: ReaderCallback) -> None: ...
    def set_close_handler(self, callback: CloseCallback) -> None: ...
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    def write(self, data: bytes) -> None: ...
    @property
    def is_open(self) -> bool: ...


class SerialTransport:
    """A real ``/dev/ttyACM*`` (or a pty, which behaves the same).

    Uses ``loop.add_reader`` on the underlying fd rather than a reader thread,
    so the whole agent stays single-threaded.
    """

    def __init__(self, port: str, baudrate: int = 115200) -> None:
        self.port = port
        self.baudrate = baudrate
        self._serial = None
        self._reader: ReaderCallback | None = None
        self._on_close: CloseCallback | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_reader(self, callback: ReaderCallback) -> None:
        self._reader = callback

    def set_close_handler(self, callback: CloseCallback) -> None:
        self._on_close = callback

    async def open(self) -> None:
        import serial  # imported lazily so tests need no hardware stack

        self._loop = asyncio.get_running_loop()
        try:
            # pyserial's open is synchronous, and opening a ttyACM that is
            # mid-enumeration can take seconds.  Run it off the loop: during
            # discovery this is called for every candidate port in turn, and
            # blocking here stalls everything else in the agent — including
            # the server link's keepalive, which then drops the connection.
            self._serial = await asyncio.to_thread(
                serial.Serial,
                self.port,
                baudrate=self.baudrate,
                timeout=0,
                write_timeout=2,
                rtscts=False,
                dsrdtr=False,
            )
        except Exception as exc:  # serial.SerialException and friends
            raise TransportClosed(f"cannot open {self.port}: {exc}") from exc

        self._serial.reset_input_buffer()
        self._loop.add_reader(self._serial.fileno(), self._on_readable)

    def _on_readable(self) -> None:
        if self._serial is None:
            return
        try:
            data = self._serial.read(4096)
        except Exception as exc:
            # Raising here would only reach the event loop's exception handler,
            # which logs it and carries on — leaving the owner of this
            # transport waiting on a port that is never going to answer again.
            # Hand the failure to whoever registered for it instead.
            self._detach()
            self._notify_closed(
                TransportClosed(f"read failed on {self.port}: {exc}")
            )
            return
        if data:
            if self._reader is not None:
                self._reader(data)
        elif self._serial is not None:
            # Readable but empty: on a tty that means the other end is gone.
            self._detach()
            self._notify_closed(TransportClosed(f"{self.port} disconnected"))

    def _notify_closed(self, exc: Exception | None) -> None:
        handler, self._on_close = self._on_close, None  # fire at most once
        if handler is not None:
            handler(exc)

    def _detach(self) -> None:
        if self._loop is not None and self._serial is not None:
            try:
                self._loop.remove_reader(self._serial.fileno())
            except (ValueError, OSError):
                pass

    async def close(self) -> None:
        self._detach()
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    def write(self, data: bytes) -> None:
        if self._serial is None:
            raise TransportClosed(f"{self.port} is not open")
        try:
            self._serial.write(data)
        except Exception as exc:
            raise TransportClosed(f"write failed on {self.port}: {exc}") from exc

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open


class PipeTransport:
    """Both ends of a link that lives inside the process.

    ``create_pair`` returns two transports wired to each other, which is how
    the mock modem is attached in tests without touching /dev.
    """

    def __init__(self, name: str = "pipe") -> None:
        self.name = name
        self._peer: PipeTransport | None = None
        self._reader: ReaderCallback | None = None
        self._on_close: CloseCallback | None = None
        self._open = False

    @classmethod
    def create_pair(cls) -> tuple["PipeTransport", "PipeTransport"]:
        a, b = cls("a"), cls("b")
        a._peer, b._peer = b, a
        return a, b

    def set_reader(self, callback: ReaderCallback) -> None:
        self._reader = callback

    def set_close_handler(self, callback: CloseCallback) -> None:
        self._on_close = callback

    async def open(self) -> None:
        self._open = True

    async def close(self) -> None:
        self._open = False

    def disconnect(self) -> None:
        """Simulate the module going away — what an unplug looks like."""
        self._open = False
        handler, self._on_close = self._on_close, None
        if handler is not None:
            handler(TransportClosed(f"{self.name} disconnected"))

    def write(self, data: bytes) -> None:
        if not self._open:
            raise TransportClosed(f"{self.name} is not open")
        peer = self._peer
        if peer is None or not peer._open or peer._reader is None:
            return
        # Deliver on the next tick so a write never re-enters the writer.
        asyncio.get_running_loop().call_soon(peer._reader, data)

    @property
    def is_open(self) -> bool:
        return self._open


class FdTransport:
    """Transport over a raw file descriptor — used for the pty master side.

    The mock modem holds the master end while the agent opens the slave path
    as an ordinary serial port, which exercises the real pyserial code path
    without hardware.
    """

    def __init__(self, fd: int, name: str = "fd") -> None:
        self.fd = fd
        self.name = name
        self._reader: ReaderCallback | None = None
        self._on_close: CloseCallback | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._open = False

    def set_reader(self, callback: ReaderCallback) -> None:
        self._reader = callback

    def set_close_handler(self, callback: CloseCallback) -> None:
        self._on_close = callback

    async def open(self) -> None:
        self._loop = asyncio.get_running_loop()
        os.set_blocking(self.fd, False)
        self._loop.add_reader(self.fd, self._on_readable)
        self._open = True

    def _on_readable(self) -> None:
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            # The peer went away; a pty master reports EIO once the slave closes.
            self._detach()
            self._notify_closed(TransportClosed(f"{self.name} disconnected: {exc}"))
            return
        if data and self._reader is not None:
            self._reader(data)

    def _notify_closed(self, exc: Exception | None) -> None:
        handler, self._on_close = self._on_close, None
        if handler is not None:
            handler(exc)

    def _detach(self) -> None:
        if self._loop is not None and self._open:
            try:
                self._loop.remove_reader(self.fd)
            except (ValueError, OSError):
                pass
        self._open = False

    async def close(self) -> None:
        self._detach()
        try:
            os.close(self.fd)
        except OSError:
            pass

    def write(self, data: bytes) -> None:
        if not self._open:
            raise TransportClosed(f"{self.name} is not open")
        try:
            os.write(self.fd, data)
        except OSError as exc:
            raise TransportClosed(f"write failed on {self.name}: {exc}") from exc

    @property
    def is_open(self) -> bool:
        return self._open


@dataclass
class PtyPair:
    """A pty whose slave path can be handed to a serial client.

    ``slave_fd`` is deliberately kept open: on Linux, reading the master of a
    pty with no slave holders fails with EIO, so something must pin it until
    the real reader attaches.
    """

    master_fd: int
    slave_fd: int
    slave_path: str

    def close(self) -> None:
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def create_pty_pair() -> PtyPair:
    master_fd, slave_fd = os.openpty()
    for fd in (master_fd, slave_fd):
        tty.setraw(fd)  # no echo, no CRLF translation — we frame it ourselves
    return PtyPair(master_fd, slave_fd, os.ttyname(slave_fd))
