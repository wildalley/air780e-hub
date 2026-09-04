"""Finding the right serial port for a module, by asking it who it is.

The USB port path a module happens to be plugged into is not its identity —
but it is all udev can see, because two Air780E modules report the same USB
serial number (``000000000001``) and IMEI lives behind AT, not in a USB
descriptor.  So binding by port path (the original decision D8) means moving a
module to another socket, or having ``/dev/ttyACM*`` renumber after a reset,
silently points a worker at the wrong hardware.

Here the agent asks instead: open each candidate port, say ``ATI`` /
``AT+CGSN`` / ``AT+ICCID``, and claim the one whose IMEI or ICCID matches what
the config named.  Ports that answer nothing (the two non-AT ACM interfaces,
or a module running LuatOS firmware) drop out on their own.

Claiming is deliberately strict: a module that cannot be identified is left
alone rather than adopted.  Keep-alive tasks send SMS from a named device, so
guessing wrong means sending from the wrong card — worse than not sending.
"""

from __future__ import annotations

import asyncio
import glob as globmodule
import logging
import os
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

from .at import ATClient, ATError, SerialTransport
from .config import DeviceConfig

log = logging.getLogger(__name__)

DEFAULT_PORT_GLOB = "/dev/ttyACM*"
# Long enough for a module still finishing its USB enumeration to answer,
# short enough that walking three ports does not stall startup.
DEFAULT_PROBE_TIMEOUT = 3.0


@dataclass(frozen=True)
class ProbeResult:
    port: str
    model: str = ""
    imei: str = ""
    iccid: str = ""

    def describe(self) -> str:
        return f"{self.port} ({self.model or '?'} imei={self.imei or '?'})"


def _identity_key(result: ProbeResult) -> str | None:
    """Return the strongest stable identity reported by a probe.

    One Air780E exposes several ``ttyACM`` interfaces.  Most firmware only
    answers AT on one of them, but some revisions answer on more than one, so
    a port is not a hardware identity.  IMEI wins; ICCID is the fallback for a
    module whose firmware does not expose ``AT+CGSN``.
    """
    imei = result.imei.strip().lower()
    if imei:
        return f"imei:{imei}"
    iccid = result.iccid.strip().lower()
    return f"iccid:{iccid}" if iccid else None


class Prober(Protocol):
    def __call__(self, port: str, *, timeout: float) -> Awaitable[ProbeResult | None]:
        ...


class NoSuchDevice(RuntimeError):
    """No connected module matches this device block — yet."""


async def probe_port(
    port: str, *, timeout: float = DEFAULT_PROBE_TIMEOUT
) -> ProbeResult | None:
    """Ask one port who it is.  ``None`` if it does not speak AT.

    Only reads: nothing here changes modem state, so probing a port that turns
    out to belong to another module is harmless.
    """
    client = ATClient(SerialTransport(port), name=f"probe:{port}")
    try:
        await client.open()
    except (ATError, OSError) as exc:
        log.debug("probe %s: cannot open (%s)", port, exc)
        return None

    async def ask(command: str) -> str:
        try:
            response = await client.execute(command, timeout=timeout)
        except ATError:
            return ""
        return response.lines[0] if response.lines else ""

    try:
        # Echo off first, or every answer arrives with the command glued in
        # front of it and the identity comparisons never match.
        await ask("ATE0")
        model = await ask("ATI")
        if not model:
            log.debug("probe %s: no answer to ATI", port)
            return None
        imei = await ask("AT+CGSN")
        iccid = (await ask("AT+ICCID")).replace("+ICCID:", "").strip()
        return ProbeResult(port=port, model=model, imei=imei.strip(), iccid=iccid)
    except Exception as exc:  # a broken port must not take the agent down
        log.debug("probe %s: %s", port, exc)
        return None
    finally:
        await client.close()


def _matches(config: DeviceConfig, found: ProbeResult) -> bool:
    """Both identifiers must agree when both are configured.

    Requiring all of them (rather than any) is what lets "this card, in this
    module" be expressed — and keeps a half-match from claiming a port.
    """
    wanted_imei = (config.imei or "").strip().lower()
    wanted_iccid = (config.iccid or "").strip().lower()
    if wanted_imei and wanted_imei != found.imei.strip().lower():
        return False
    if wanted_iccid and wanted_iccid != found.iccid.strip().lower():
        return False
    return bool(wanted_imei or wanted_iccid)


class PortRegistry:
    """Hands each worker a port, and makes sure no two get the same one.

    One instance is shared by every worker.  The lock matters for more than
    bookkeeping: two workers probing at once would have their AT traffic
    interleaved on ports neither owns yet.
    """

    def __init__(
        self,
        *,
        port_glob: str = DEFAULT_PORT_GLOB,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
        prober: Prober | None = None,
        sole_device: bool = False,
    ) -> None:
        self.port_glob = port_glob
        self.probe_timeout = probe_timeout
        self._probe: Prober = prober or probe_port
        # With exactly one module configured and no identity given, "the port
        # that answers" is unambiguous, so allow it.  With two, it is not.
        self.sole_device = sole_device
        self._lock = asyncio.Lock()
        self._claimed: dict[str, str] = {}  # port -> device name

    def claimed_by(self, port: str) -> str | None:
        return self._claimed.get(port)

    async def acquire(self, config: DeviceConfig) -> str:
        async with self._lock:
            candidates = [
                port
                for port in sorted(globmodule.glob(self.port_glob))
                if port not in self._claimed
            ]
            if not candidates:
                raise NoSuchDevice(
                    f"no unclaimed port matches {self.port_glob!r}"
                )

            identified = config.imei or config.iccid
            for port in candidates:
                found = await self._probe(port, timeout=self.probe_timeout)
                if found is None:
                    continue
                if identified and not _matches(config, found):
                    log.debug(
                        "[%s] %s is not ours (imei=%s iccid=%s)",
                        config.name, port, found.imei or "?", found.iccid or "?",
                    )
                    continue
                if not identified and not self.sole_device:
                    # Unreachable via config validation, but a wrong claim is
                    # expensive enough to refuse here too.
                    raise NoSuchDevice(
                        f"device {config.name!r} has no imei/iccid and is not "
                        "the only device — refusing to guess"
                    )
                self._claimed[port] = config.name
                log.info("[%s] claimed %s", config.name, found.describe())
                return port

            raise NoSuchDevice(
                f"device {config.name!r} not found among "
                + ", ".join(candidates)
            )

    def release(self, port: str) -> None:
        self._claimed.pop(port, None)

    async def survey(self, reserved: list[DeviceConfig]) -> list[ProbeResult]:
        """Identify unclaimed modules that no configured device is waiting for.

        This is the autodetect half of discovery, and it is deliberately the
        mirror image of :meth:`acquire`: that one starts from a device block
        and hunts for its module, this one starts from a module and asks
        whether anybody has a claim on it.

        A module is only reported when every ``reserved`` block would reject
        it.  Adopting one that a configured device is merely waiting for —
        unplugged at this moment, or still enumerating — would take the name
        that device is entitled to and send keep-alive SMS from the wrong
        card, which is the failure this module exists to prevent.

        Ports named outright by a pinned device are skipped without probing.
        Pinned workers bypass the registry, so their ports never appear in
        ``_claimed``, and probing one would mean opening a tty another worker
        already holds.

        Probing does not claim: the caller decides what to adopt, and a port
        left unadopted stays available to :meth:`acquire`.
        """
        # Resolved, because a pinned port is usually a udev symlink and the
        # glob yields the tty it points at.
        pinned = {
            os.path.realpath(config.port)
            for config in reserved
            if config.is_pinned
        }
        async with self._lock:
            found: list[ProbeResult] = []
            seen_identities: set[str] = set()
            for port in sorted(globmodule.glob(self.port_glob)):
                if port in self._claimed or port in pinned:
                    continue
                result = await self._probe(port, timeout=self.probe_timeout)
                if result is None:
                    continue
                if any(_matches(config, result) for config in reserved):
                    log.debug(
                        "survey: %s is spoken for by a configured device", port
                    )
                    continue
                identity = _identity_key(result)
                if identity is not None and identity in seen_identities:
                    log.debug(
                        "survey: %s is another AT port for %s; ignoring duplicate",
                        port,
                        identity,
                    )
                    continue
                found.append(result)
                if identity is not None:
                    seen_identities.add(identity)
            return found
