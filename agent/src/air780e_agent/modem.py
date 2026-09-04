"""High-level Air780E driver.

Wraps :class:`ATClient` in the operations the rest of the agent actually
wants — read the inbox, send a message, sample the signal — and owns the
policy that keeps messages from being lost:

    +CMTI arrives -> AT+CMGR the index -> reassemble -> hand upstream
                  -> AT+CMGD to free the slot

The delete is not optional.  Storage is small — an AirM2M_780EPV_V1011
reported 10 slots, for both "SM" and "ME" — and a full store makes the network
drop new messages silently, so a slot that is not released is a message that
will not arrive.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .at import ATClient, ATCommandError, ATError, ATUrc, CmsError
from .pdu import (
    DecodedSms,
    PduError,
    Reassembler,
    StatusReport,
    decode_pdu,
    decode_status_report,
    encode_submit,
)

log = logging.getLogger(__name__)

SmsCallback = Callable[[DecodedSms], None | Awaitable[None]]
DeliveryCallback = Callable[[StatusReport], None | Awaitable[None]]
CallCallback = Callable[["IncomingCall"], None | Awaitable[None]]

# Report the caller's number alongside RING.  Optional and diagnostic: a module
# that refuses it still reports the call itself, just anonymously.
CLIP_ENABLE = "AT+CLIP=1"

# RING repeats every ~5s for as long as the caller waits.  Two RINGs further
# apart than this are treated as separate calls; closer together, as one call
# still ringing.
RING_REPEAT_GAP = 12.0
# +CLIP follows its RING almost immediately, so a short wait is enough to
# attach the caller's number to the record instead of logging it as anonymous.
CLIP_GRACE = 1.0

# 2 = forward URCs even while the link is reserved; 1 = store the message and
# report only its index.  Storing (rather than +CMT push) means a message
# survives the agent being restarted mid-delivery.
CNMI_STORE_AND_NOTIFY = "AT+CNMI=2,1,0,1,0"

# Ask the module to *push* a URC whenever registration changes, so a SIM that
# drops off and comes back updates `registered` without waiting for the next
# status poll.  Mode 1 reports the bare stat; mode 2 adds location/act fields.
# We use 1 for the widest module support — `_on_registration` copes with both.
CREG_ENABLE = "AT+CREG=1"   # circuit-switched / 2G
CEREG_ENABLE = "AT+CEREG=1"  # EPS / LTE
CIREG_ENABLE = "AT+CIREG=1"  # IMS, optional and diagnostic only

SEND_TIMEOUT = 60.0

# Bounds a `+CBC` reading has to fall inside to be believed, in millivolts.
# The module runs on a nominal 3.3-4.2V supply, so anything outside this is a
# field this parser has misread rather than a real measurement — the `80` of a
# `+CBC: 0,80` charge-percentage reply being the case that matters.
VOLTAGE_PLAUSIBLE_MV = (2000, 6000)

# A voice keep-alive dials, waits just long enough for the network to start
# ringing the far end, then hangs up.  Long enough that the carrier books a
# call attempt, short enough that nobody actually picks up — an answered call
# would bill the user and, worse, ring a real phone at whatever hour the
# scheduler chose.
CALL_RING_SECONDS = 8.0
# ``ATD`` normally answers OK as soon as call setup starts, but firmware is
# free to hold the line until the call ends instead, which is why this allows
# for the whole ring window plus the network's own setup time.
DIAL_TIMEOUT = 45.0
HANGUP_TIMEOUT = 15.0
# How often to ask the module what the call is actually doing while it rings.
CALL_POLL_INTERVAL = 1.5

# ATD takes a raw dial string that is written straight into the AT stream, so
# anything that could carry a carriage return has to be refused rather than
# escaped: a "number" containing \r would end the dial command and run the
# rest as a command of its own.  Digits plus the DTMF/dial characters GSM
# 27.007 allows are enough for a real number.
_DIALABLE_RE = re.compile(r"^\+?[0-9*#ABCD]{3,20}$")

# +CLCC <stat> values.  Only these three say the network engaged with the call.
CALL_STATE_ACTIVE = 0
CALL_STATE_DIALING = 2
CALL_STATE_ALERTING = 3

# Final result codes ATD can end on.  Each says the call reached the network
# and the network answered for the far end, which is exactly what a keep-alive
# needs — so they are outcomes to record, not failures to raise.  The AT client
# turns every one of them into ATCommandError, hence the text lookup.
_CALL_PROGRESS_OUTCOMES = {
    "BUSY": ("busy", True),
    "NO ANSWER": ("no_answer", True),
    # Ambiguous on purpose: the far end released the call, or the module never
    # got it out of the door.  +CLCC evidence decides which, so this one does
    # not claim the network was reached on its own.
    "NO CARRIER": ("released", False),
    "NO DIALTONE": ("no_dialtone", False),
}

# A network scan can take several minutes while the modem listens for every
# supported operator.  Keep this separate from the ordinary AT command
# timeout so callers cannot accidentally put ``AT+COPS=?`` on the short path.
OPERATOR_SCAN_TIMEOUT = 180.0
OPERATOR_SELECT_TIMEOUT = 180.0

# Manual selection is asynchronous.  V1011 answers ``AT+COPS=1,2,"<MCCMNC>"``
# with OK as soon as it accepts the request — measured at 0s — and only settles
# on a network seconds later; a 46001 selection that the SIM could not hold was
# observed falling back to 46000 about 15s after the OK.  Reading ``AT+COPS?``
# straight off that OK therefore snapshots the module mid-switch, as a bare
# ``+COPS: 1`` with no operator field, which reads on the device page as "the
# selection did nothing".  So wait for registration to settle before taking the
# snapshot.  These bound that wait and are unrelated to the timeouts above,
# which are the AT timeouts for a single command.
OPERATOR_SETTLE_TIMEOUT = 27.0
OPERATOR_SETTLE_INTERVAL = 3.0

# States that mean "attached to the network": 1 = home, 5 = roaming.
REGISTERED_STATES = ("1", "5")


def _timestamp() -> str:
    # Same shape the worker stamps its events with, so an incoming call sorts
    # against messages without any conversion on the way through.
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _tpdu_octets(pdu: str) -> int | None:
    """TPDU length in octets — what ``+CMGR``/``+CMGL`` report, SMSC excluded.

    ``None`` when the string is not usable hex, in which case there is nothing
    to cross-check and decoding will raise on its own.
    """
    try:
        raw = bytes.fromhex(pdu)
    except ValueError:
        return None
    if not raw or len(raw) < 1 + raw[0]:
        return None
    return len(raw) - 1 - raw[0]


def _declared_octets(header: str) -> int | None:
    """The ``<length>`` field of a ``+CMGR:`` / ``+CMGL:`` header.

    It is last in both, so the same parse serves both.  A quoted alpha field
    containing a comma would confuse the split — that yields ``None`` and the
    cross-check is skipped rather than reporting a bogus mismatch.
    """
    fields = [field.strip() for field in header.split(",")]
    try:
        return int(fields[-1])
    except (ValueError, IndexError):
        return None


@dataclass
class ModemInfo:
    model: str = ""
    manufacturer: str = ""
    hardware_model: str = ""
    firmware: str = ""
    imei: str = ""
    iccid: str = ""
    smsc: str = ""
    operator: str = ""
    registered: bool = False
    eps_registered: bool | None = None
    cs_registered: bool | None = None
    ims_registered: bool | None = None
    radio_enabled: bool | None = None
    data_attached: bool | None = None
    pdp_active: bool | None = None
    roaming: bool | None = None


@dataclass
class Signal:
    rssi: int | None = None  # 0..31 as reported by +CSQ
    ber: int | None = None
    rsrp: int | None = None
    rsrq: int | None = None

    @property
    def dbm(self) -> int | None:
        """+CSQ scale -> dBm.  99 means 'not known or not detectable'."""
        if self.rssi is None or self.rssi >= 99:
            return None
        return -113 + 2 * self.rssi

    @property
    def bars(self) -> int:
        dbm = self.dbm
        if dbm is None:
            return 0
        for threshold, bars in ((-75, 5), (-85, 4), (-95, 3), (-105, 2)):
            if dbm >= threshold:
                return bars
        return 1


@dataclass
class CallResult:
    """What happened to one outgoing keep-alive call.

    ``reached_network`` is the field callers should judge success by, not the
    absence of an exception.  A keep-alive succeeds when the carrier saw a call
    attempt, and the codes that prove that (``BUSY``, ``NO ANSWER``, an
    alerting ``+CLCC``) all arrive as errors from the AT layer.
    """

    outcome: str
    reached_network: bool
    ring_seconds: float = 0.0
    detail: str = ""
    states: list[int] = field(default_factory=list)

    def describe(self) -> str:
        label = {
            "alerting": "far end rang",
            "answered": "answered (hung up immediately)",
            "busy": "far end busy",
            "no_answer": "no answer",
            "dialing": "call set up",
            "released": "released before ringing",
            "no_dialtone": "no dial tone",
            "no_progress": "never left the module",
        }.get(self.outcome, self.outcome)
        suffix = f"; {self.detail}" if self.detail else ""
        return f"{label} after {self.ring_seconds:.1f}s{suffix}"


@dataclass
class IncomingCall:
    """A ``RING``/``+CLIP`` notification, recorded rather than answered."""

    number: str = ""
    ts: str = ""


@dataclass
class StoredIndex:
    index: int
    stat: int
    pdu: str


def _csv_fields(value: str) -> list[str]:
    """Parse a modem CSV value while preserving quoted commas."""
    try:
        return [field.strip() for field in next(csv.reader([value], skipinitialspace=True))]
    except (csv.Error, StopIteration):
        return []


def parse_current_operator(value: str) -> dict[str, int | str | None]:
    """Parse the value after ``+COPS:`` from a query response.

    The operator name is only present when the modem is registered or has a
    selected operator.  Missing fields are deliberately represented as
    ``None`` rather than guessed values.
    """
    if value.lstrip().upper().startswith("+COPS:"):
        value = value.split(":", 1)[1]
    fields = _csv_fields(value)
    out: dict[str, int | str | None] = {
        "mode": None,
        "format": None,
        "operator": "",
        "numeric": "",
        "access_technology": None,
    }
    if not fields:
        return out
    try:
        out["mode"] = int(fields[0])
    except ValueError:
        return out
    if len(fields) < 2:
        return out
    try:
        out["format"] = int(fields[1])
    except ValueError:
        return out
    if len(fields) >= 3:
        operator = fields[2].strip()
        if out["format"] == 2 and re.fullmatch(r"[0-9]{5,6}", operator):
            out["operator"] = operator
            out["numeric"] = operator
        else:
            out["operator"] = operator
    if len(fields) >= 4:
        # With format 2 the third field is the numeric operator; with other
        # formats it is the optional AcT field.
        if out["format"] == 2 and re.fullmatch(r"[0-9]{5,6}", fields[2].strip()):
            out["numeric"] = fields[2].strip()
            try:
                out["access_technology"] = int(fields[3])
            except ValueError:
                pass
        else:
            try:
                out["access_technology"] = int(fields[3])
            except ValueError:
                pass
    return out


def _operator_groups(value: str) -> list[str]:
    """Extract balanced ``(...)`` entries from an ``AT+COPS=?`` response."""
    groups: list[str] = []
    start: int | None = None
    depth = 0
    quoted = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quoted:
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if quoted:
            continue
        if char == "(":
            if depth == 0:
                start = index + 1
            depth += 1
        elif char == ")" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                groups.append(value[start:index])
                start = None
    return groups


def parse_operator_scan(value: str) -> list[dict[str, int | str | None]]:
    """Parse standard ``+COPS: (stat,long,short,numeric,AcT),...`` data."""
    operators: list[dict[str, int | str | None]] = []
    seen: set[str] = set()
    for group in _operator_groups(value):
        fields = _csv_fields(group)
        if len(fields) < 4:
            continue
        numeric = fields[3].strip()
        if not re.fullmatch(r"[0-9]{5,6}", numeric):
            continue
        try:
            status = int(fields[0])
        except ValueError:
            status = None
        try:
            access_technology: int | None = int(fields[4]) if len(fields) > 4 else None
        except ValueError:
            access_technology = None
        if numeric in seen:
            continue
        seen.add(numeric)
        operators.append(
            {
                "status": status,
                "long_name": fields[1].strip().strip('"'),
                "short_name": fields[2].strip().strip('"'),
                "numeric": numeric,
                "access_technology": access_technology,
            }
        )
    return operators


class Air780E:
    def __init__(
        self,
        client: ATClient,
        *,
        on_sms: SmsCallback | None = None,
        on_delivery: DeliveryCallback | None = None,
        on_call: CallCallback | None = None,
        storage: str = "SM",
        delete_after_read: bool = True,
        reassembly_timeout: float = 30.0,
    ) -> None:
        self.client = client
        self.on_sms = on_sms
        self.on_delivery = on_delivery
        self.on_call = on_call
        self.storage = storage
        self.delete_after_read = delete_after_read
        self.info = ModemInfo()
        self.operator_selection_mode: int | None = None

        self._reassembler = Reassembler(timeout=reassembly_timeout)
        self._flush_task: asyncio.Task | None = None
        self._new_message_indexes: asyncio.Queue[int] = asyncio.Queue()
        self._drain_task: asyncio.Task | None = None
        # RING repeats for as long as the caller waits, so the report is held
        # briefly (for the +CLIP that carries the number) and the repeats are
        # collapsed into one.  `_ring_seen` starts far enough in the past that
        # the very first RING is never mistaken for a repeat.
        self._ring_call: IncomingCall | None = None
        self._ring_seen: float = -RING_REPEAT_GAP
        self._ring_task: asyncio.Task | None = None

        client.register_urc("+CMTI", self._on_cmti)
        client.register_urc("+CMT", self._on_cmt, payload_lines=1)
        client.register_urc("+CDS", self._on_cds, payload_lines=1)
        client.register_urc("+CREG", self._on_registration)
        client.register_urc("+CEREG", self._on_registration)
        client.register_urc("+CIREG", self._on_registration)
        # Incoming calls are recorded, never answered.  RING carries no caller
        # ID; +CLIP does, and arrives alongside it once AT+CLIP=1 is set.
        client.register_urc("RING", self._on_ring)
        client.register_urc("+CLIP", self._on_clip)

    # -- setup -------------------------------------------------------------

    async def initialize(self) -> ModemInfo:
        """Put the modem into the state the rest of the agent assumes."""
        await self.client.execute("ATE0")  # echo off: halves the parsing work
        # Numeric errors preserve +CMS/+CME codes for diagnosis.  CMEE=2 turns
        # them into firmware-specific text and loses the machine-readable code.
        await self.client.execute("AT+CMEE=1")
        await self.client.execute("AT+CMGF=0")  # PDU mode, always
        await self.client.execute("AT+CSCS=\"GSM\"")

        try:
            await self.client.execute(
                f'AT+CPMS="{self.storage}","{self.storage}","{self.storage}"'
            )
        except ATError as exc:
            log.warning("could not select storage %s: %s", self.storage, exc)

        await self.client.execute(CNMI_STORE_AND_NOTIFY)

        # Turn on unsolicited registration reporting.  Not every module
        # implements both domains, and a missing one must not abort setup —
        # the periodic refresh in `read_registration` covers whatever URCs
        # never arrive.
        # CLIP is in the same list for the same reason: caller ID is a nicety,
        # and a module that refuses it still reports the call itself.
        for command in (CREG_ENABLE, CEREG_ENABLE, CIREG_ENABLE, CLIP_ENABLE):
            try:
                await self.client.execute(command)
            except ATError as exc:
                log.debug("%s not accepted: %s", command, exc)

        self.info = await self.read_info()
        self._start_background()
        return self.info

    def _start_background(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._index_loop())

    async def close(self) -> None:
        for task in (self._flush_task, self._drain_task, self._ring_task):
            if task is not None:
                task.cancel()
        self._flush_task = None
        self._drain_task = None
        self._ring_task = None

    # -- information -------------------------------------------------------

    async def read_info(self) -> ModemInfo:
        info = ModemInfo()

        async def quiet(command: str) -> str | None:
            try:
                response = await self.client.execute(command)
            except ATError as exc:
                log.debug("%s failed: %s", command, exc)
                return None
            return response.lines[0] if response.lines else None

        info.model = await quiet("ATI") or ""
        info.manufacturer = await quiet("AT+CGMI") or ""
        info.hardware_model = await quiet("AT+CGMM") or ""
        info.firmware = await quiet("AT+CGMR") or ""
        info.imei = await quiet("AT+CGSN") or ""

        iccid = await quiet("AT+ICCID")
        if iccid:
            info.iccid = iccid.replace("+ICCID:", "").strip()

        smsc = await quiet("AT+CSCA?")
        if smsc and (match := re.search(r'"([^"]+)"', smsc)):
            info.smsc = match.group(1)

        cops = await quiet("AT+COPS?")
        if cops:
            current_operator = parse_current_operator(cops)
            info.operator = str(current_operator["operator"] or "")
            mode = current_operator["mode"]
            self.operator_selection_mode = mode if isinstance(mode, int) else None

        info.radio_enabled = await self.read_radio_enabled()
        if info.radio_enabled is False:
            info.eps_registered = False
            info.cs_registered = False
            info.roaming = False
            info.data_attached = False
            info.pdp_active = False
        else:
            (
                info.eps_registered,
                info.cs_registered,
                info.roaming,
            ) = await self.read_registration_details()
            info.data_attached, info.pdp_active = await self.read_data_status()
        info.registered = bool(info.eps_registered or info.cs_registered)
        info.ims_registered = await self.read_ims_registration()

        return info

    async def read_current_operator(self) -> dict[str, int | str | None]:
        """Return the standard ``AT+COPS?`` selection fields."""
        response = await self.client.execute("AT+COPS?")
        value = response.first("+COPS:")
        current = parse_current_operator(value or "")
        mode = current["mode"]
        self.operator_selection_mode = mode if isinstance(mode, int) else None
        return current

    async def scan_operators(self) -> list[dict[str, int | str | None]]:
        """Scan visible operators using the documented ``AT+COPS=?`` query."""
        response = await self.client.execute(
            "AT+COPS=?", timeout=OPERATOR_SCAN_TIMEOUT
        )
        values = response.all("+COPS:")
        return parse_operator_scan(",".join(values))

    async def select_operator(
        self, numeric: str | None
    ) -> dict[str, int | str | None]:
        """Select a numeric operator, or restore automatic selection.

        Only MCC/MNC values are accepted here.  This keeps the typed command
        from becoming an unvalidated escape hatch into arbitrary AT syntax.

        The OK for the selection means "request accepted", not "switched", so
        this waits for the module to reattach before reporting — otherwise the
        answer is a mid-switch ``+COPS: 1`` that looks like nothing happened.
        Staying unregistered for the whole window is a real outcome, not an
        error: the caller gets the honest searching snapshot, plus ``settled``
        to say the wait ran out.
        """
        if numeric is None:
            command = "AT+COPS=0"
        else:
            if not re.fullmatch(r"[0-9]{5,6}", numeric):
                raise ValueError("operator numeric must contain 5 or 6 digits")
            command = f'AT+COPS=1,2,"{numeric}"'
        await self.client.execute(command, timeout=OPERATOR_SELECT_TIMEOUT)
        settled = await self._await_registration(
            OPERATOR_SETTLE_TIMEOUT, OPERATOR_SETTLE_INTERVAL
        )
        current = await self.read_current_operator()
        # The mode the operator asked for, not the one the mid-switch snapshot
        # happens to report — this is what suspends automatic recovery, and it
        # must not be undone by a module that answered `+COPS: 0` while still
        # acting on a manual request.
        self.operator_selection_mode = 0 if numeric is None else 1
        current["settled"] = settled
        return current

    async def _await_registration(self, timeout: float, interval: float) -> bool:
        """Poll until either domain reports registered, or the window closes.

        Returns whether registration was seen.  The first read happens before
        any sleep so an already-attached module is not made to wait out an
        interval it does not need.
        """
        deadline = time.monotonic() + timeout
        while True:
            if await self.read_registration():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(interval, remaining))

    async def read_network_diagnostics(self) -> dict[str, dict[str, list[str] | str | None]]:
        """Read optional engineering diagnostics without imposing a schema.

        These commands differ across Air780E firmware releases.  Preserve their
        raw lines so a newer firmware remains useful even when it adds fields
        the agent does not know yet, and so an unsupported command degrades to
        one missing section instead of failing the whole read.

        ``AT+CCED`` takes ``<mode>,<dump>`` and has no bare execute form: V1011
        answers the parameterless spelling with ``+CME ERROR: 3`` (operation not
        allowed), which reads like a permission problem but only means the
        arguments are missing.  ``AT+CCED=?`` reports ``(0,1,2),(1,2,8)``; mode
        0 is the one-shot read, dump 1 the serving cell and dump 2 the
        neighbours.  Mode 2 stops periodic reporting, so it is never sent here.

        ``AT*BANDIND?`` and ``AT^SYSINFO`` are queries only.  Their *set* forms
        are not band or cell locking: V1011 accepts just ``*BANDIND=(0,1)`` and
        ``^SYSCONFIG`` mode ``(2)``, neither of which can express "lock to band
        N".  See docs/at-reference.md §2.2.

        Note that the serving-cell line carries the IMSI, so these lines are as
        sensitive as the ICCID the device page already shows.
        """
        diagnostics: dict[str, dict[str, list[str] | str | None]] = {}
        for key, command in (
            ("cced", "AT+CCED=0,1"),
            ("cced_neighbors", "AT+CCED=0,2"),
            # Documented by Luat but absent from V1011: +EEMGINFO, ^EEMGINFO,
            # *EEMGINFO and +EMGINFO all answer a bare ERROR, with no test form
            # either.  Kept for firmware that does implement it — an
            # unsupported command costs one empty section, not the whole read.
            ("eemginfo", "AT+EEMGINFO"),
            ("bandind", "AT*BANDIND?"),
            ("sysinfo", "AT^SYSINFO"),
        ):
            try:
                response = await self.client.execute(command, timeout=30.0)
            except ATError as exc:
                diagnostics[key] = {"lines": [], "error": str(exc)}
            else:
                diagnostics[key] = {"lines": response.lines, "error": None}
        return diagnostics

    async def read_radio_enabled(self) -> bool | None:
        """Return whether RF is enabled, or ``None`` if the firmware cannot say.

        Air780E reports ``1`` for full functionality and ``0`` for minimum
        functionality.  Some modem families also use ``4`` for flight mode;
        anything other than ``1`` therefore means that cellular RF is off.
        """
        try:
            response = await self.client.execute("AT+CFUN?")
        except ATError as exc:
            log.debug("AT+CFUN? failed: %s", exc)
            return None
        value = response.first("+CFUN:") or ""
        match = re.match(r"\s*(\d+)", value)
        return None if match is None else match.group(1) == "1"

    async def set_radio_enabled(self, enabled: bool) -> tuple[bool, bool]:
        """Enable or disable cellular RF and return ``(enabled, registered)``.

        ``AT+CFUN=0`` leaves the AT port alive, which is essential: the same
        connection must remain available to turn RF back on.  Reattachment is
        asynchronous after ``AT+CFUN=1``; the immediate registration read is
        reported honestly and the worker's regular status poll follows it to
        the eventual registered state.
        """
        await self.client.execute(f"AT+CFUN={1 if enabled else 0}", timeout=30.0)
        self.info.radio_enabled = enabled
        if enabled:
            self.info.registered = await self.read_registration()
            self.info.ims_registered = await self.read_ims_registration()
        else:
            self.info.registered = False
            self.info.eps_registered = False
            self.info.cs_registered = False
            self.info.roaming = False
            self.info.data_attached = False
            self.info.pdp_active = False
            if self.info.ims_registered is not None:
                self.info.ims_registered = False
        return enabled, self.info.registered

    async def read_data_status(self) -> tuple[bool | None, bool | None]:
        """Return ``(packet attached, any PDP context active)``.

        ``CGATT`` and ``CGACT`` are separate states.  A module can be attached
        without an active data context, so reporting only one of them would
        make an apparently disabled data switch ambiguous.
        """
        attached: bool | None = None
        try:
            response = await self.client.execute("AT+CGATT?")
        except ATError as exc:
            log.debug("AT+CGATT? failed: %s", exc)
        else:
            value = response.first("+CGATT:") or ""
            match = re.match(r"\s*(\d+)", value)
            if match is not None:
                attached = match.group(1) == "1"

        pdp_active: bool | None = None
        try:
            response = await self.client.execute("AT+CGACT?")
        except ATError as exc:
            log.debug("AT+CGACT? failed: %s", exc)
        else:
            states: list[bool] = []
            for value in response.all("+CGACT:"):
                match = re.match(r"\s*\d+\s*,\s*(\d+)", value)
                if match is not None:
                    states.append(match.group(1) == "1")
            if states:
                pdp_active = any(states)

        self.info.data_attached = attached
        self.info.pdp_active = pdp_active
        return attached, pdp_active

    async def set_data_enabled(self, enabled: bool) -> tuple[bool | None, bool | None]:
        """Allow packet data, or stop user data without dropping registration.

        ``CGATT`` and ``CGACT`` are different controls.  Detaching packet
        service with ``AT+CGATT=0`` can also disturb EPS registration (and, on
        some networks, IMS registration), while the thing that can carry user
        traffic is an active PDP context.  Therefore the safe ``False`` path
        deactivates PDP contexts but deliberately keeps packet attachment when
        possible.  If an older Agent already detached the module, reattach it
        first and deactivate again afterwards: this restores the normal
        registration state without leaving a data context active.

        Enabling only attaches packet service — it does not invent an APN or
        activate a context that the host did not request.
        """
        errors: list[ATError] = []
        if enabled:
            await self.client.execute("AT+CGATT=1", timeout=30.0)
        else:
            attached, active = await self.read_data_status()

            # Stop an already active context before changing attachment.  A
            # detached module normally reports no active context, but keeping
            # this branch makes the order safe for firmware that reports stale
            # state during reconnect.
            if active is not False:
                try:
                    await self.client.execute("AT+CGACT=0", timeout=30.0)
                except ATError as exc:
                    errors.append(exc)

            # Reattach only the packet-service control plane.  Some firmware
            # may reject this while the modem is still searching; that is not a
            # data-safety failure as long as the final PDP query is explicitly
            # inactive.  If it succeeds, deactivate once more because a modem
            # may create a default context while attaching.
            if attached is not True:
                try:
                    await self.client.execute("AT+CGATT=1", timeout=30.0)
                except ATError as exc:
                    errors.append(exc)
                else:
                    try:
                        await self.client.execute("AT+CGACT=0", timeout=30.0)
                    except ATError as exc:
                        errors.append(exc)

        state = await self.read_data_status()
        if not enabled and state[1] is not False:
            attached, active = state
            raise ATError(
                "PDP deactivation was not confirmed "
                f"(attached={attached!r}, pdp_active={active!r})",
                command="AT+CGACT=0",
            )
        if not enabled and errors:
            # The post-command state is the safety guarantee.  Keep the
            # non-fatal command failures visible for diagnostics, but do not
            # take an otherwise healthy Agent offline merely because the card
            # was temporarily unregistered and rejected CGATT=1.
            log.warning(
                "data disable command warning(s): %s",
                "; ".join(str(error) for error in errors),
            )
        return state

    async def _read_registration_domain(
        self, command: str, *prefixes: str
    ) -> bool | None:
        """Read one 3GPP registration domain, preserving unsupported/unknown.

        Several prefixes may be given because firmware does not always answer
        under the prefix it was asked about: Air780E V1011 answers ``AT+CEREG?``
        with ``+CGREG: 0,5``.  Accepting the alias is what keeps the EPS domain
        from reading as "unknown" forever on that firmware.
        """
        registered, _ = await self._read_registration_domain_state(command, *prefixes)
        return registered

    async def _read_registration_domain_state(
        self, command: str, *prefixes: str
    ) -> tuple[bool | None, bool | None]:
        """Return ``(registered, roaming)`` while preserving unknown values."""
        try:
            response = await self.client.execute(command)
        except ATError as exc:
            log.debug("%s failed: %s", command, exc)
            return None, None
        for prefix in prefixes:
            value = response.first(f"+{prefix}:")
            if value is None:
                continue
            # Query form is "<n>,<stat>[,...]"; the stat is the second field.
            match = re.match(r"\s*\d+\s*,\s*(\d+)", value)
            if match is not None:
                state = match.group(1)
                return state in REGISTERED_STATES, state == "5"
        return None, None

    async def read_registration_domains(self) -> tuple[bool | None, bool | None]:
        """Return ``(EPS/LTE, CS)`` registration without collapsing the evidence."""
        eps, cs, _ = await self.read_registration_details()
        return eps, cs

    async def read_registration_details(
        self,
    ) -> tuple[bool | None, bool | None, bool | None]:
        """Return EPS, CS and roaming state from the registration domains."""
        # ``+CGREG`` is deliberately not a registered URC prefix: `_handle_line`
        # matches the in-flight command's expected prefix *before* the URC
        # router, so routing it as a URC would take this very reply out of the
        # response and put the domain back to unknown.  A pushed EPS change is
        # instead picked up by the next status poll.
        eps, eps_roaming = await self._read_registration_domain_state(
            "AT+CEREG?", "CEREG", "CGREG"
        )
        cs, cs_roaming = await self._read_registration_domain_state(
            "AT+CREG?", "CREG"
        )
        roaming_values = [
            value for value in (eps_roaming, cs_roaming) if value is not None
        ]
        roaming = (
            True
            if any(roaming_values)
            else False
            if roaming_values
            else None
        )
        return eps, cs, roaming

    async def read_registration(self) -> bool:
        """True if the module is registered on *either* the CS or EPS domain.

        Checking only ``AT+CEREG?`` (LTE/EPS) misses a SIM that fell back to
        2G and is registered on the circuit-switched domain reported by
        ``AT+CREG?`` — common when roaming, which is exactly when a foreign
        SIM like giffgaff lands here.  ``1`` is home, ``5`` is roaming; both
        mean "attached".
        """
        eps, cs, roaming = await self.read_registration_details()
        self.info.eps_registered = eps
        self.info.cs_registered = cs
        self.info.roaming = roaming
        self.info.registered = bool(eps or cs)
        return self.info.registered

    async def read_ims_registration(self) -> bool | None:
        """Return IMS registration, or ``None`` when firmware does not expose it.

        ``AT+CIREG?`` is diagnostic only.  An unregistered IMS domain must not
        block ``AT+CMGS``: some networks carry SMS over NAS/SGs without IMS.
        Air780E firmware varies, so rejection of the query is represented as
        unknown instead of being mistaken for an unregistered service.
        """
        return await self._read_registration_domain("AT+CIREG?", "CIREG")

    async def recover_registration(self) -> bool:
        """Nudge a module that is stuck unregistered back onto the network.

        Re-selects the operator automatically, and if that alone does not take,
        cycles the radio with ``AT+CFUN``.  Returns the registration state
        afterwards.  Cycling the radio drops any data session, so callers
        should reserve this for a module that has stayed unregistered rather
        than firing it on the first missed sample.
        """
        if await self.reselect_operator():
            return True

        return await self.cycle_radio()

    async def reselect_operator(self) -> bool:
        """Ask the module to resume automatic operator selection."""
        if not await self._automatic_recovery_allowed():
            return False

        try:
            await self.client.execute("AT+COPS=0", timeout=30.0)
        except ATError as exc:
            log.warning("AT+COPS=0 failed during recovery: %s", exc)

        registered = await self.read_registration()
        self.info.registered = registered
        return registered

    async def cycle_radio(self) -> bool:
        """Cycle RF while keeping the AT port alive, then check attachment."""
        if not await self._automatic_recovery_allowed():
            return False

        try:
            await self.client.execute("AT+CFUN=0", timeout=30.0)
            await asyncio.sleep(1.0)
            await self.client.execute("AT+CFUN=1", timeout=30.0)
        except ATError as exc:
            log.warning("AT+CFUN cycle failed during recovery: %s", exc)
            registered = await self.read_registration()
            self.info.registered = registered
            return registered

        # The radio needs a moment to reattach after CFUN=1.
        await asyncio.sleep(3.0)
        registered = await self.read_registration()
        self.info.registered = registered
        return registered

    async def reset(self) -> None:
        """Restart the module; the worker deliberately reconnects afterwards."""
        await self.client.execute("AT+RESET", timeout=30.0)

    async def _automatic_recovery_allowed(self) -> bool:
        radio_enabled = await self.read_radio_enabled()
        if radio_enabled is None:
            # Keep the last known deliberate state when a diagnostic query
            # itself times out; never turn RF back on based on an unknown read.
            radio_enabled = self.info.radio_enabled
        if radio_enabled is not False:
            return True
        # A deliberate flight-mode choice must never be undone by an
        # automatic registration recovery.
        self.info.radio_enabled = False
        self.info.registered = False
        self.info.eps_registered = False
        self.info.cs_registered = False
        if self.info.ims_registered is not None:
            self.info.ims_registered = False
        return False

    async def read_signal(self) -> Signal:
        signal = Signal()
        try:
            response = await self.client.execute("AT+CSQ")
        except ATError:
            return signal
        if value := response.first("+CSQ:"):
            parts = [p.strip() for p in value.split(",")]
            if parts and parts[0].isdigit():
                signal.rssi = int(parts[0])
            if len(parts) > 1 and parts[1].isdigit():
                signal.ber = int(parts[1])

        try:
            response = await self.client.execute("AT+CESQ")
        except ATError:
            return signal
        if value := response.first("+CESQ:"):
            parts = [p.strip() for p in value.split(",")]
            # +CESQ: rxlev,ber,rscp,ecno,rsrq,rsrp
            if len(parts) >= 6:
                if parts[4].isdigit() and int(parts[4]) != 255:
                    signal.rsrq = int(parts[4])
                if parts[5].isdigit() and int(parts[5]) != 255:
                    signal.rsrp = int(parts[5])
        return signal

    async def read_voltage(self) -> int | None:
        """Supply voltage in millivolts, or ``None`` if the module will not say.

        Two response shapes are in the wild and the field count tells them
        apart.  GSM 27.007 defines ``+CBC: <bcs>,<bcl>,<voltage>``, while
        Air780E ``V1011`` answers with the millivolt figure on its own
        (``+CBC: 3968`` measured).  Reading by position would take the ``80``
        of a ``0,80`` reply as 80 mV, so a value outside a plausible supply
        range is discarded rather than reported.
        """
        try:
            response = await self.client.execute("AT+CBC")
        except ATError as exc:
            log.debug("AT+CBC failed: %s", exc)
            return None
        value = response.first("+CBC:")
        if value is None:
            return None
        numbers = [int(n) for n in re.findall(r"\d+", value)]
        if not numbers:
            return None
        # One field is the voltage; three put it last.  Anything else is a
        # shape this parser does not claim to know.
        if len(numbers) == 1:
            millivolts = numbers[0]
        elif len(numbers) >= 3:
            millivolts = numbers[2]
        else:
            return None
        if not VOLTAGE_PLAUSIBLE_MV[0] <= millivolts <= VOLTAGE_PLAUSIBLE_MV[1]:
            log.debug("ignoring implausible +CBC voltage: %s", value)
            return None
        return millivolts

    async def storage_usage(self) -> tuple[int, int]:
        """(used, capacity) for the active message store."""
        try:
            response = await self.client.execute("AT+CPMS?")
        except ATError:
            return (0, 0)
        value = response.first("+CPMS:") or ""
        numbers = re.findall(r"(\d+)", value)
        if len(numbers) >= 2:
            return (int(numbers[0]), int(numbers[1]))
        return (0, 0)

    # -- inbox -------------------------------------------------------------

    async def list_stored(self, stat: int = 4) -> list[StoredIndex]:
        """``AT+CMGL`` — everything currently in the modem's store."""
        response = await self.client.execute(f"AT+CMGL={stat}", timeout=30.0)
        out: list[StoredIndex] = []
        lines = response.lines
        for i, line in enumerate(lines):
            if not line.upper().startswith("+CMGL:"):
                continue
            header = line.split(":", 1)[1]
            fields = [f.strip() for f in header.split(",")]
            if i + 1 >= len(lines):
                log.warning("+CMGL header with no PDU line: %s", line)
                continue
            pdu = lines[i + 1].strip()
            declared = _declared_octets(header)
            actual = _tpdu_octets(pdu)
            if declared is not None and actual is not None and declared != actual:
                # Kept rather than dropped: a short body still beats no message
                # at all, and the log is what makes the loss visible.
                log.error(
                    "+CMGL index %s: declared %d TPDU octet(s), read %d — "
                    "the message body is probably truncated",
                    fields[0] if fields else "?", declared, actual,
                )
            try:
                out.append(
                    StoredIndex(
                        index=int(fields[0]),
                        stat=int(fields[1]) if len(fields) > 1 else 0,
                        pdu=pdu,
                    )
                )
            except (ValueError, IndexError):
                log.warning("unparsable +CMGL header: %s", line)
        return out

    async def read_stored(self, index: int, *, retry: bool = True) -> str | None:
        try:
            response = await self.client.execute(f"AT+CMGR={index}")
        except CmsError as exc:
            if exc.code in (321, 322):  # invalid index — already gone
                return None
            raise
        for i, line in enumerate(response.lines):
            if line.upper().startswith("+CMGR:") and i + 1 < len(response.lines):
                pdu = response.lines[i + 1].strip()
                declared = _declared_octets(line.split(":", 1)[1])
                actual = _tpdu_octets(pdu)
                # DIAG(readpath): the raw read-out PDU before any decoding, so
                # a short body can be pinned to CMGR rather than the decoder.
                log.info(
                    "DIAG cmgr index=%d header=%r declared=%s actual=%s "
                    "hexlen=%d pdu=%s",
                    index, line, declared, actual, len(pdu), pdu,
                )
                if declared is None or actual is None or declared == actual:
                    return pdu

                # The modem says how long the TPDU is; a PDU shorter than that
                # decodes into a *truncated body with no error anywhere* —
                # seen once on real hardware, and silence is the worst way to
                # lose half a verification code.
                log.warning(
                    "index %d: +CMGR declared %d TPDU octet(s), read %d — re-reading",
                    index, declared, actual,
                )
                if retry:
                    again = await self.read_stored(index, retry=False)
                    if again is not None and _tpdu_octets(again) == declared:
                        return again
                log.error(
                    "index %d: PDU still %d/%d octet(s) after re-reading; "
                    "the message body is probably truncated",
                    index, actual, declared,
                )
                return pdu
        return None

    async def delete_stored(self, index: int) -> None:
        try:
            await self.client.execute(f"AT+CMGD={index}")
        except CmsError as exc:
            if exc.code not in (321, 322):
                raise

    async def drain_inbox(self) -> list[DecodedSms]:
        """Read and clear everything sitting in the modem's store.

        Run this at startup: it recovers messages that arrived while the agent
        was down, and — just as importantly — frees the slots so the store
        does not stay full.
        """
        collected: list[DecodedSms] = []
        for stored in await self.list_stored():
            try:
                sms = decode_pdu(stored.pdu)
            except PduError as exc:
                log.error("undecodable PDU at index %d: %s", stored.index, exc)
                if self.delete_after_read:
                    await self.delete_stored(stored.index)
                continue

            complete = self._reassembler.push(sms)
            if complete is not None:
                collected.append(complete)
            if self.delete_after_read:
                await self.delete_stored(stored.index)

        # Anything still partial after a full sweep has no siblings coming.
        collected.extend(self._reassembler.flush_expired())
        for sms in collected:
            await self._emit(sms)
        return collected

    # -- sending -----------------------------------------------------------

    async def send_sms(self, number: str, text: str) -> list[int]:
        """Send one message, splitting into segments when needed.

        Returns the message reference of each segment.  A failure partway
        through a multipart message raises, and the references already sent
        are lost from the caller's view — the caller decides whether a retry
        is safe (the scheduler's keep-alive messages are idempotent enough).
        """
        references: list[int] = []
        for part in encode_submit(number, text):
            response = await self.client.execute(
                f"AT+CMGS={part.tpdu_len}",
                payload=part.pdu_hex + "\x1a",
                expect_prompt=True,
                timeout=SEND_TIMEOUT,
            )
            value = response.first("+CMGS:")
            references.append(int(value) if value and value.isdigit() else -1)
        return references

    # -- voice -------------------------------------------------------------

    async def call_keepalive(
        self, number: str, *, ring_seconds: float = CALL_RING_SECONDS
    ) -> CallResult:
        """Dial ``number``, let it ring briefly, then hang up.

        Some plans count a call attempt but not an SMS, so this exists purely
        to make the carrier's billing system see activity.  It deliberately
        never lets the call be answered: the far end is usually the user's own
        second number, and the scheduler may fire at 04:00.

        Returns a :class:`CallResult` rather than raising for the codes that
        mean the call reached the network.  ``ATD`` ends on ``BUSY``,
        ``NO ANSWER`` or ``NO CARRIER``, all of which the AT layer reports as
        :class:`ATCommandError` — for a keep-alive those are the *successful*
        shapes, so unpicking them here is the whole point of the method.  What
        still raises is a module that never dialled at all (``+CME ERROR``,
        ``ERROR``, a dead port), because that is a fault to retry.
        """
        if not _DIALABLE_RE.match(number.strip()):
            # Refused rather than escaped: ATD's argument goes into the AT
            # stream verbatim, so a value carrying \r would end the dial and
            # run whatever followed as its own command.
            raise ValueError(f"not a dialable number: {number!r}")

        dialed = number.strip()
        started = time.monotonic()
        states: list[int] = []
        outcome: str | None = None
        reached = False
        detail = ""

        try:
            # ``;`` makes this a voice call.  Without it the module tries a
            # data call, which either fails outright or, worse, connects and
            # keeps the AT link captured in data mode.
            await self.client.execute(f"ATD{dialed};", timeout=DIAL_TIMEOUT)
        except ATCommandError as exc:
            known = _CALL_PROGRESS_OUTCOMES.get(exc.final)
            if known is None:
                raise
            outcome, reached = known
            detail = exc.final
        # Any other ATError (CmeError, ATTimeout, TransportClosed) propagates:
        # the call never happened and the scheduler should treat it as failure.

        if outcome is None:
            # ATD returned OK, so the call is up and it is on us to end it.
            # Poll +CLCC while it rings: the state the network reached is the
            # only positive evidence that the attempt was real, and it is gone
            # once we hang up.
            try:
                states = await self._watch_call(started, ring_seconds)
            finally:
                await self.hangup()
            if CALL_STATE_ACTIVE in states:
                outcome, reached = "answered", True
            elif CALL_STATE_ALERTING in states:
                outcome, reached = "alerting", True
            elif CALL_STATE_DIALING in states:
                # Set-up started but never reached the far end within the ring
                # window.  Worth recording, but it does not prove the carrier
                # booked an attempt, so it does not count as reaching them.
                outcome, reached = "dialing", False
            else:
                # ATD said OK yet +CLCC never listed a call.  Seen when the
                # network rejects setup immediately; the module reports success
                # for the dial itself.
                outcome, reached = "no_progress", False
                detail = "+CLCC never reported a call"
        elif outcome == "released":
            # NO CARRIER with no +CLCC evidence either way.  Treated as not
            # reaching the network, so a card that silently fails every call
            # is not reported as a healthy keep-alive.
            detail = f"{detail} (no ringing observed)"

        result = CallResult(
            outcome=outcome,
            reached_network=reached,
            ring_seconds=time.monotonic() - started,
            detail=detail,
            states=states,
        )
        log.info("[call] %s -> %s", dialed, result.describe())
        return result

    async def _watch_call(self, started: float, ring_seconds: float) -> list[int]:
        """Collect the ``+CLCC`` states seen while the call rings.

        Polls before sleeping, and keeps the interval short enough that several
        polls fit the window: the state right after ``ATD`` is the one most
        likely to be missed, and sleeping a fixed 1.5s first would both lose it
        and overshoot any window shorter than the interval.
        """
        states: list[int] = []
        interval = min(CALL_POLL_INTERVAL, max(ring_seconds / 4, 0.05))
        while True:
            try:
                response = await self.client.execute("AT+CLCC")
            except ATError as exc:
                # A module that will not report call state is not a reason to
                # leave a call up; stop watching and let the caller hang up.
                log.debug("AT+CLCC failed mid-call: %s", exc)
                break
            listed = False
            for value in response.all("+CLCC:"):
                fields = _csv_fields(value)
                if len(fields) >= 3 and fields[2].isdigit():
                    states.append(int(fields[2]))
                    listed = True
            if not listed and states:
                # The call left the list: it ended on its own (far end
                # rejected, network released).  Nothing more to observe.
                break
            if time.monotonic() - started >= ring_seconds:
                break
            await asyncio.sleep(interval)
        return states

    async def hangup(self) -> None:
        """End whatever call is up.

        Never raises.  This runs in the cleanup path of a keep-alive, and a
        failure here must not mask the outcome that path is reporting — but a
        call left up would keep billing, so it is worth a warning.
        """
        try:
            await self.client.execute("ATH", timeout=HANGUP_TIMEOUT)
        except ATError as exc:
            log.warning("ATH failed, call may still be up: %s", exc)

    async def ping(self, host: str = "www.baidu.com") -> bool:
        """Burn a few bytes of data — some carriers want traffic, not just SMS."""
        try:
            await self.client.execute(f'AT+CIPPING="{host}"', timeout=30.0)
            return True
        except ATError as exc:
            log.warning("ping %s failed: %s", host, exc)
            return False

    # -- URC handling ------------------------------------------------------

    def _on_cmti(self, urc: ATUrc) -> None:
        # +CMTI: "SM",5   — a message landed in slot 5.
        match = re.search(r",\s*(\d+)", urc.params)
        if match:
            self._new_message_indexes.put_nowait(int(match.group(1)))
        else:
            log.warning("unparsable +CMTI: %s", urc.params)

    async def _on_cmt(self, urc: ATUrc) -> None:
        # +CMT: ,23 followed by the PDU — only seen with CNMI mode 2,2.
        if not urc.payload:
            return
        try:
            sms = decode_pdu(urc.payload[0])
        except PduError as exc:
            log.error("undecodable +CMT PDU: %s", exc)
            return
        complete = self._reassembler.push(sms)
        if complete is not None:
            await self._emit(complete)

    async def _on_cds(self, urc: ATUrc) -> None:
        # PDU mode: +CDS: <length>, followed by one SMS-STATUS-REPORT PDU.
        if not urc.payload:
            log.warning("+CDS arrived without a status-report PDU")
            return
        try:
            report = decode_status_report(urc.payload[0])
        except PduError as exc:
            log.error("undecodable +CDS PDU: %s", exc)
            return
        if self.on_delivery is None:
            return
        result = self.on_delivery(report)
        if asyncio.iscoroutine(result):
            await result

    def _on_ring(self, urc: ATUrc) -> None:
        """Note an incoming call.  Never answered — only recorded.

        A single call makes the module emit ``RING`` every few seconds until it
        stops, so this collapses the repeats into one record: reporting per line
        would turn one missed call into a dozen log entries.
        """
        now = time.monotonic()
        # Gate on when a RING was last seen, not on whether a report is still
        # pending: the pending call is cleared after CLIP_GRACE, so a phone that
        # rings for twenty seconds would otherwise be reported over and over.
        recent = now - self._ring_seen < RING_REPEAT_GAP
        self._ring_seen = now
        if recent:
            return
        self._ring_call = IncomingCall(ts=_timestamp())
        # Wait briefly before reporting: +CLIP carries the caller's number and
        # arrives just after RING, so reporting immediately would record every
        # call as anonymous.
        self._ring_task = asyncio.get_running_loop().create_task(self._report_call())

    def _on_clip(self, urc: ATUrc) -> None:
        # +CLIP: "13800138000",129,,,,0
        match = re.match(r'\s*"?([+0-9*#]+)"?', urc.params)
        number = match.group(1) if match else ""
        if self._ring_call is not None:
            if number:
                self._ring_call.number = number
            return
        if time.monotonic() - self._ring_seen < RING_REPEAT_GAP:
            # The call this belongs to has already been reported; a repeat +CLIP
            # from the same ringing call must not become a second record.
            return
        # +CLIP with no RING in front of it: report it as its own call rather
        # than dropping the only notice we got.
        self._ring_seen = time.monotonic()
        self._ring_call = IncomingCall(number=number, ts=_timestamp())
        self._ring_task = asyncio.get_running_loop().create_task(self._report_call())

    async def _report_call(self) -> None:
        """Hand the pending incoming call upstream once +CLIP has had time."""
        try:
            await asyncio.sleep(CLIP_GRACE)
        except asyncio.CancelledError:
            return
        call, self._ring_call = self._ring_call, None
        if call is None or self.on_call is None:
            return
        log.info(
            "[call] incoming from %s", call.number or "unknown number"
        )
        try:
            result = self.on_call(call)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("incoming-call handler failed")

    def _on_registration(self, urc: ATUrc) -> None:
        # An unsolicited +CREG/+CEREG/+CIREG is stat-first: "+CREG: 1" or, in mode 2,
        # "+CREG: 1,\"00C3\",\"1234ABCD\",7".  (The query form "<n>,<stat>" is
        # consumed by the pending AT+CxREG? command, never routed here.)  The
        # earlier "<n>,<stat>" pattern silently marked a registered module as
        # unregistered whenever a mode-2 URC carried location fields.
        match = re.match(r"\s*(\d+)", urc.params)
        state = match.group(1) if match else urc.params.strip()
        registered = state in REGISTERED_STATES
        if urc.name.upper() == "+CIREG":
            self.info.ims_registered = registered
            return
        if urc.name.upper() == "+CEREG":
            self.info.eps_registered = registered
        else:
            self.info.cs_registered = registered
        self.info.registered = bool(
            self.info.eps_registered or self.info.cs_registered
        )

    async def _index_loop(self) -> None:
        """Serialize +CMTI follow-ups so two URCs cannot interleave reads."""
        while True:
            index = await self._new_message_indexes.get()
            try:
                await self._fetch_index(index)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("failed to fetch message at index %d", index)

    async def _diag_dump_store(self, index: int) -> None:
        """DIAG(readpath): raw ``AT+CMGL`` dump, to compare store vs read-out.

        Runs after the ``+CMGR`` for ``index`` and before the ``AT+CMGD``, on
        the same serialized ``_index_loop`` turn, so it cannot interleave with
        another fetch.  Never raises: a diagnostic must not cost a message.
        """
        try:
            response = await self.client.execute("AT+CMGL=4", timeout=30.0)
        except Exception as exc:  # noqa: BLE001 — diagnostics stay silent
            log.info("DIAG cmgl for index=%d failed: %r", index, exc)
            return
        log.info("DIAG cmgl after index=%d: %d line(s)", index, len(response.lines))
        for i, line in enumerate(response.lines):
            if line.upper().startswith("+CMGL:"):
                header = line.split(":", 1)[1]
                pdu = response.lines[i + 1].strip() if i + 1 < len(response.lines) else ""
                log.info(
                    "DIAG cmgl header=%r declared=%s actual=%s hexlen=%d pdu=%s",
                    line, _declared_octets(header), _tpdu_octets(pdu), len(pdu), pdu,
                )

    async def _fetch_index(self, index: int) -> None:
        pdu = await self.read_stored(index)
        if pdu is None:
            log.warning("index %d vanished before it could be read", index)
            return
        await self._diag_dump_store(index)
        try:
            sms = decode_pdu(pdu)
        except PduError as exc:
            log.error("undecodable PDU at index %d: %s", index, exc)
            if self.delete_after_read:
                await self.delete_stored(index)
            return

        complete = self._reassembler.push(sms)
        if self.delete_after_read:
            await self.delete_stored(index)
        if complete is not None:
            await self._emit(complete)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            for sms in self._reassembler.flush_expired():
                log.warning(
                    "multipart message from %s timed out; emitting %d chars",
                    sms.address, len(sms.text),
                )
                await self._emit(sms)

    async def _emit(self, sms: DecodedSms) -> None:
        if self.on_sms is None:
            return
        result = self.on_sms(sms)
        if asyncio.iscoroutine(result):
            await result
