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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .at import ATClient, ATError, ATUrc, CmsError
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
# A network scan can take several minutes while the modem listens for every
# supported operator.  Keep this separate from the ordinary AT command
# timeout so callers cannot accidentally put ``AT+COPS=?`` on the short path.
OPERATOR_SCAN_TIMEOUT = 180.0
OPERATOR_SELECT_TIMEOUT = 180.0

# States that mean "attached to the network": 1 = home, 5 = roaming.
REGISTERED_STATES = ("1", "5")


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
        storage: str = "SM",
        delete_after_read: bool = True,
        reassembly_timeout: float = 30.0,
    ) -> None:
        self.client = client
        self.on_sms = on_sms
        self.on_delivery = on_delivery
        self.storage = storage
        self.delete_after_read = delete_after_read
        self.info = ModemInfo()
        self.operator_selection_mode: int | None = None

        self._reassembler = Reassembler(timeout=reassembly_timeout)
        self._flush_task: asyncio.Task | None = None
        self._new_message_indexes: asyncio.Queue[int] = asyncio.Queue()
        self._drain_task: asyncio.Task | None = None

        client.register_urc("+CMTI", self._on_cmti)
        client.register_urc("+CMT", self._on_cmt, payload_lines=1)
        client.register_urc("+CDS", self._on_cds, payload_lines=1)
        client.register_urc("+CREG", self._on_registration)
        client.register_urc("+CEREG", self._on_registration)
        client.register_urc("+CIREG", self._on_registration)

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
        for command in (CREG_ENABLE, CEREG_ENABLE, CIREG_ENABLE):
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
        for task in (self._flush_task, self._drain_task):
            if task is not None:
                task.cancel()
        self._flush_task = None
        self._drain_task = None

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
        else:
            info.eps_registered, info.cs_registered = await self.read_registration_domains()
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
        """
        if numeric is None:
            command = "AT+COPS=0"
        else:
            if not re.fullmatch(r"[0-9]{5,6}", numeric):
                raise ValueError("operator numeric must contain 5 or 6 digits")
            command = f'AT+COPS=1,2,"{numeric}"'
        await self.client.execute(command, timeout=OPERATOR_SELECT_TIMEOUT)
        current = await self.read_current_operator()
        self.operator_selection_mode = 0 if numeric is None else 1
        return current

    async def read_network_diagnostics(self) -> dict[str, dict[str, list[str] | str | None]]:
        """Read optional engineering diagnostics without imposing a schema.

        These commands differ across Air780E firmware releases.  Preserve their
        raw lines so a newer firmware remains useful even when it adds fields
        the agent does not know yet, and so an unsupported command degrades to
        one missing section instead of failing the whole read.

        ``AT*BANDIND?`` and ``AT^SYSINFO`` are queries only.  Their *set* forms
        are not band or cell locking: V1011 accepts just ``*BANDIND=(0,1)`` and
        ``^SYSCONFIG`` mode ``(2)``, neither of which can express "lock to band
        N".  See docs/at-reference.md §2.2.
        """
        diagnostics: dict[str, dict[str, list[str] | str | None]] = {}
        for key, command in (
            ("cced", "AT+CCED"),
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
            if self.info.ims_registered is not None:
                self.info.ims_registered = False
        return enabled, self.info.registered

    async def _read_registration_domain(
        self, command: str, *prefixes: str
    ) -> bool | None:
        """Read one 3GPP registration domain, preserving unsupported/unknown.

        Several prefixes may be given because firmware does not always answer
        under the prefix it was asked about: Air780E V1011 answers ``AT+CEREG?``
        with ``+CGREG: 0,5``.  Accepting the alias is what keeps the EPS domain
        from reading as "unknown" forever on that firmware.
        """
        try:
            response = await self.client.execute(command)
        except ATError as exc:
            log.debug("%s failed: %s", command, exc)
            return None
        for prefix in prefixes:
            value = response.first(f"+{prefix}:")
            if value is None:
                continue
            # Query form is "<n>,<stat>[,...]"; the stat is the second field.
            match = re.match(r"\s*\d+\s*,\s*(\d+)", value)
            if match is not None:
                return match.group(1) in REGISTERED_STATES
        return None

    async def read_registration_domains(self) -> tuple[bool | None, bool | None]:
        """Return ``(EPS/LTE, CS)`` registration without collapsing the evidence."""
        # ``+CGREG`` is deliberately not a registered URC prefix: `_handle_line`
        # matches the in-flight command's expected prefix *before* the URC
        # router, so routing it as a URC would take this very reply out of the
        # response and put the domain back to unknown.  A pushed EPS change is
        # instead picked up by the next status poll.
        eps = await self._read_registration_domain("AT+CEREG?", "CEREG", "CGREG")
        cs = await self._read_registration_domain("AT+CREG?", "CREG")
        return eps, cs

    async def read_registration(self) -> bool:
        """True if the module is registered on *either* the CS or EPS domain.

        Checking only ``AT+CEREG?`` (LTE/EPS) misses a SIM that fell back to
        2G and is registered on the circuit-switched domain reported by
        ``AT+CREG?`` — common when roaming, which is exactly when a foreign
        SIM like giffgaff lands here.  ``1`` is home, ``5`` is roaming; both
        mean "attached".
        """
        eps, cs = await self.read_registration_domains()
        self.info.eps_registered = eps
        self.info.cs_registered = cs
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

    async def _fetch_index(self, index: int) -> None:
        pdu = await self.read_stored(index)
        if pdu is None:
            log.warning("index %d vanished before it could be read", index)
            return
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
