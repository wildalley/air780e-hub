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
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .at import ATClient, ATError, ATUrc, CmsError
from .pdu import DecodedSms, PduError, Reassembler, decode_pdu, encode_submit

log = logging.getLogger(__name__)

SmsCallback = Callable[[DecodedSms], None | Awaitable[None]]

# 2 = forward URCs even while the link is reserved; 1 = store the message and
# report only its index.  Storing (rather than +CMT push) means a message
# survives the agent being restarted mid-delivery.
CNMI_STORE_AND_NOTIFY = "AT+CNMI=2,1,0,0,0"

# Ask the module to *push* a URC whenever registration changes, so a SIM that
# drops off and comes back updates `registered` without waiting for the next
# status poll.  Mode 1 reports the bare stat; mode 2 adds location/act fields.
# We use 1 for the widest module support — `_on_registration` copes with both.
CREG_ENABLE = "AT+CREG=1"   # circuit-switched / 2G
CEREG_ENABLE = "AT+CEREG=1"  # EPS / LTE

SEND_TIMEOUT = 60.0

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
    imei: str = ""
    iccid: str = ""
    smsc: str = ""
    operator: str = ""
    registered: bool = False


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


class Air780E:
    def __init__(
        self,
        client: ATClient,
        *,
        on_sms: SmsCallback | None = None,
        storage: str = "SM",
        delete_after_read: bool = True,
        reassembly_timeout: float = 30.0,
    ) -> None:
        self.client = client
        self.on_sms = on_sms
        self.storage = storage
        self.delete_after_read = delete_after_read
        self.info = ModemInfo()

        self._reassembler = Reassembler(timeout=reassembly_timeout)
        self._flush_task: asyncio.Task | None = None
        self._new_message_indexes: asyncio.Queue[int] = asyncio.Queue()
        self._drain_task: asyncio.Task | None = None

        client.register_urc("+CMTI", self._on_cmti)
        client.register_urc("+CMT", self._on_cmt, payload_lines=1)
        client.register_urc("+CREG", self._on_registration)
        client.register_urc("+CEREG", self._on_registration)

    # -- setup -------------------------------------------------------------

    async def initialize(self) -> ModemInfo:
        """Put the modem into the state the rest of the agent assumes."""
        await self.client.execute("ATE0")  # echo off: halves the parsing work
        await self.client.execute("AT+CMEE=2")  # verbose errors, not bare ERROR
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
        for command in (CREG_ENABLE, CEREG_ENABLE):
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
        info.imei = await quiet("AT+CGSN") or ""

        iccid = await quiet("AT+ICCID")
        if iccid:
            info.iccid = iccid.replace("+ICCID:", "").strip()

        smsc = await quiet("AT+CSCA?")
        if smsc and (match := re.search(r'"([^"]+)"', smsc)):
            info.smsc = match.group(1)

        cops = await quiet("AT+COPS?")
        if cops and (match := re.search(r'"([^"]+)"', cops)):
            info.operator = match.group(1)

        info.registered = await self.read_registration()

        return info

    async def read_registration(self) -> bool:
        """True if the module is registered on *either* the CS or EPS domain.

        Checking only ``AT+CEREG?`` (LTE/EPS) misses a SIM that fell back to
        2G and is registered on the circuit-switched domain reported by
        ``AT+CREG?`` — common when roaming, which is exactly when a foreign
        SIM like giffgaff lands here.  ``1`` is home, ``5`` is roaming; both
        mean "attached".
        """
        for command, prefix in (("AT+CEREG?", "CEREG"), ("AT+CREG?", "CREG")):
            try:
                response = await self.client.execute(command)
            except ATError as exc:
                log.debug("%s failed: %s", command, exc)
                continue
            value = response.first(f"+{prefix}:") or ""
            # Query form is "<n>,<stat>[,...]"; the stat is the second field.
            match = re.match(r"\s*\d+\s*,\s*(\d+)", value)
            if match and match.group(1) in REGISTERED_STATES:
                return True
        return False

    async def recover_registration(self) -> bool:
        """Nudge a module that is stuck unregistered back onto the network.

        Re-selects the operator automatically, and if that alone does not take,
        cycles the radio with ``AT+CFUN``.  Returns the registration state
        afterwards.  Cycling the radio drops any data session, so callers
        should reserve this for a module that has stayed unregistered rather
        than firing it on the first missed sample.
        """
        try:
            await self.client.execute("AT+COPS=0", timeout=30.0)
        except ATError as exc:
            log.warning("AT+COPS=0 failed during recovery: %s", exc)

        if await self.read_registration():
            return True

        try:
            await self.client.execute("AT+CFUN=0", timeout=30.0)
            await asyncio.sleep(1.0)
            await self.client.execute("AT+CFUN=1", timeout=30.0)
        except ATError as exc:
            log.warning("AT+CFUN cycle failed during recovery: %s", exc)
            return await self.read_registration()

        # The radio needs a moment to reattach after CFUN=1.
        await asyncio.sleep(3.0)
        registered = await self.read_registration()
        self.info.registered = registered
        return registered

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

    def _on_registration(self, urc: ATUrc) -> None:
        # An unsolicited +CREG/+CEREG is stat-first: "+CREG: 1" or, in mode 2,
        # "+CREG: 1,\"00C3\",\"1234ABCD\",7".  (The query form "<n>,<stat>" is
        # consumed by the pending AT+CxREG? command, never routed here.)  The
        # earlier "<n>,<stat>" pattern silently marked a registered module as
        # unregistered whenever a mode-2 URC carried location fields.
        match = re.match(r"\s*(\d+)", urc.params)
        state = match.group(1) if match else urc.params.strip()
        self.info.registered = state in ("1", "5")

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
