"""A fake Air780E that speaks AT, so the whole agent can be built and tested
before the hardware arrives.

Response formats follow the openLuat AT manual
(https://docs.openluat.com/air780e/at/app/at_command).  The parts worth
imitating faithfully are the awkward ones:

* ``AT+CMGS`` is two-step — it answers ``> `` and waits for a Ctrl-Z body.
* Message storage is *small and finite*.  When it fills up, new messages are
  silently dropped by the network side.  That is the single biggest
  data-loss risk in this project, so the mock reproduces it and the agent is
  tested against it.
* URCs arrive whenever they feel like it, including mid-command.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .at.errors import CME_ERRORS, CMS_ERRORS
from .at.transport import Transport
from .pdu import codec

log = logging.getLogger(__name__)

CRLF = "\r\n"

STAT_REC_UNREAD = 0
STAT_REC_READ = 1

DEFAULT_MODEL = "AirM2M_780E_V1171_LTE_AT"


@dataclass
class StoredMessage:
    index: int
    stat: int
    pdu: str

    @property
    def tpdu_len(self) -> int:
        """Length ``+CMGL``/``+CMGR`` report: TPDU octets, excluding the SMSC."""
        raw = bytes.fromhex(self.pdu)
        return len(raw) - 1 - raw[0]


@dataclass
class MockAir780E:
    transport: Transport

    model: str = DEFAULT_MODEL
    manufacturer: str = "AirM2M"
    hardware_model: str = "Air780EPV"
    firmware: str = "V1011"
    imei: str = "867567048825499"
    iccid: str = "89860622180012345678"
    smsc: str = "+8613800210500"
    operator: str = "CHINA MOBILE"
    operator_numeric: str = "46000"
    scanned_operators: list[tuple[int, str, str, str, int]] = field(
        default_factory=lambda: [
            (1, "CHINA MOBILE", "CMCC", "46000", 7),
            (2, "CHINA UNICOM", "UNICOM", "46001", 7),
        ]
    )
    # Shapes measured on AirM2M_780EPV_V1011 (2026-08-18).  The third field of
    # the serving-cell line is the IMSI; it is masked here so no fixture
    # carries a real subscriber identity.
    cced_lines: list[str] = field(
        default_factory=lambda: [
            "+CCED:LTE current cell: 460,00,000000000000000,1,3,5,1300,164654196,63,14,37289,31,442"
        ]
    )
    cced_neighbour_lines: list[str] = field(
        default_factory=lambda: [
            "+CCED:LTE neighbor cell: 460,00,38400,185861504,57,8,37289,65535,369",
            "+CCED:LTE neighbor cell: 460,00,36275,185861438,45,10,37289,65535,371",
        ]
    )
    # V1011 does not implement AT+EEMGINFO in any spelling; even the test form
    # answers bare ERROR.  Kept so a firmware that does implement it still
    # reports, and so the unsupported path stays covered.
    eemginfo_supported: bool = False
    eemginfo_lines: list[str] = field(
        default_factory=lambda: ["+EEMGINFO: LTE,46000,7,55,20"]
    )
    # Both query-only reads; the field layouts are undocumented, hence kept as
    # raw lines.
    bandind_lines: list[str] = field(default_factory=lambda: ["*BANDIND: 0, 39, 7"])
    sysinfo_lines: list[str] = field(
        default_factory=lambda: ["^SYSINFO: 2,2,1,17,1,7"]
    )

    # Storage is deliberately tiny by default — that is what a SIM gives you.
    # 10 is what an AirM2M_780EPV_V1011 actually reported, for both "SM" and
    # "ME" (measured 2026-08-03); the 20~50 assumed while planning was
    # optimistic.
    capacity: int = 10
    storage: str = "SM"

    rssi: int = 24  # 0..31, 99 = unknown
    rsrp: int = 55  # +CESQ encoding
    rsrq: int = 20
    registered: bool = True
    ims_registered: bool = False
    radio_enabled: bool = True
    pin_ready: bool = True

    # Failure injection for tests.
    fail_next_send: bool = False
    unsupported: set[str] = field(default_factory=set)
    silent: set[str] = field(default_factory=set)
    # Air780E V1011 quirks, both measured on real hardware (2026-08-18):
    # errors come back as text even under +CMEE=1, and AT+CEREG? answers under
    # the +CGREG prefix.
    force_text_errors: bool = False
    cereg_answers_as_cgreg: bool = False
    cops_recovers_registration: bool = True
    cfun_recovers_registration: bool = True
    reset_recovers_registration: bool = True
    # Chop this many hex characters off the PDU that +CMGR returns, while the
    # header keeps advertising the full length.  Real hardware did this once:
    # the body came back short with no error on the wire.  Counts down, so 1
    # means "corrupt the next read only".
    truncate_reads: int = 0

    _messages: dict[int, StoredMessage] = field(default_factory=dict)
    _next_index: int = 1
    _next_mr: int = 0
    _echo: bool = False
    _cmee: int = 2
    _cmgf: int = 0
    _cnmi: str = ""
    _pending_send: int | None = None
    _send_buffer: str = ""
    _buffer: bytearray = field(default_factory=bytearray)
    sent: list[codec.DecodedSms] = field(default_factory=list)
    pings: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    reset_count: int = 0

    def __post_init__(self) -> None:
        self.transport.set_reader(self._feed)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        await self.transport.open()

    async def stop(self) -> None:
        await self.transport.close()

    # -- test/console hooks ------------------------------------------------

    def deliver(self, sender: str, text: str, *, when: datetime | None = None) -> bool:
        """Simulate an incoming SMS.

        Returns False and drops the message when storage is full, exactly as a
        real SIM does — no URC is emitted, so the agent never learns about it.
        """
        pdus = codec.encode_deliver(sender, text, when=when)
        if len(self._messages) + len(pdus) > self.capacity:
            log.warning("mock storage full (%d/%d), dropping message",
                        len(self._messages), self.capacity)
            return False
        for pdu in pdus:
            index = self._next_index
            self._next_index += 1
            self._messages[index] = StoredMessage(index, STAT_REC_UNREAD, pdu)
            self._urc(f'+CMTI: "{self.storage}",{index}')
        return True

    @property
    def stored_count(self) -> int:
        return len(self._messages)

    def fill_storage(self, count: int) -> None:
        """Preload junk so tests can drive the near-full boundary."""
        for i in range(count):
            self.deliver("10086", f"filler {i}")

    def report_delivery(
        self, reference: int, recipient: str, *, status: int = 0
    ) -> None:
        """Emit a PDU-mode ``+CDS`` delivery report."""
        pdu = codec.encode_status_report(reference, recipient, status=status)
        tpdu_len = len(bytes.fromhex(pdu)) - 1 - bytes.fromhex(pdu)[0]
        self._urc(f"+CDS: {tpdu_len}")
        self._write(f"{pdu}{CRLF}")

    # -- wire --------------------------------------------------------------

    def _write(self, text: str) -> None:
        self.transport.write(text.encode())

    def _urc(self, line: str) -> None:
        self._write(f"{CRLF}{line}{CRLF}")

    def _reply(self, lines: list[str] | None = None, final: str = "OK") -> None:
        out = "".join(f"{CRLF}{line}{CRLF}" for line in (lines or []))
        self._write(f"{out}{CRLF}{final}{CRLF}")

    def _error(self, cms: int | None = None, cme: int | None = None) -> None:
        if cms is not None:
            self._reply(final=f"+CMS ERROR: {self._error_value('CMS', cms)}")
        elif cme is not None and self._cmee:
            self._reply(final=f"+CME ERROR: {self._error_value('CME', cme)}")
        else:
            self._reply(final="ERROR")

    def _error_value(self, family: str, code: int) -> str:
        """The code, or its name when emulating firmware that ignores CMEE=1."""
        if not self.force_text_errors:
            return str(code)
        table = CMS_ERRORS if family == "CMS" else CME_ERRORS
        return table.get(code, str(code))

    def _feed(self, data: bytes) -> None:
        self._buffer.extend(data)

        # A pending AT+CMGS swallows everything up to Ctrl-Z as the PDU body.
        if self._pending_send is not None:
            text = self._buffer.decode("utf-8", errors="replace")
            if "\x1a" in text:
                body, _, rest = text.partition("\x1a")
                self._buffer = bytearray(rest.encode())
                self._finish_send(self._send_buffer + body)
            elif "\x1b" in text:  # ESC aborts the send
                self._buffer.clear()
                self._pending_send = None
                self._send_buffer = ""
                self._reply(final="OK")
            else:
                self._send_buffer += text
                self._buffer.clear()
            return

        while True:
            index = self._buffer.find(b"\r")
            if index < 0:
                break
            line = self._buffer[:index].decode("utf-8", errors="replace").strip()
            del self._buffer[: index + 1]
            if line:
                if self._echo:
                    self._write(line + "\r")
                self._dispatch(line)

    # -- command dispatch --------------------------------------------------

    def _dispatch(self, line: str) -> None:
        upper = line.upper()
        self.commands.append(upper)

        if upper in {name.upper() for name in self.silent}:
            return

        for name in self.unsupported:
            if upper.startswith(name.upper()):
                self._error(cme=4)
                return

        if upper == "AT":
            return self._reply()
        if upper == "ATI":
            return self._reply([self.model])
        if upper == "AT+CGMI":
            return self._reply([self.manufacturer])
        if upper == "AT+CGMM":
            return self._reply([self.hardware_model])
        if upper in ("ATE0", "ATE1"):
            self._echo = upper.endswith("1")
            return self._reply()
        if upper.startswith("AT+CMEE="):
            self._cmee = int(upper.split("=", 1)[1] or 0)
            return self._reply()
        if upper.startswith("AT+CMGF="):
            self._cmgf = int(upper.split("=", 1)[1] or 0)
            return self._reply()
        if upper.startswith("AT+CNMI="):
            self._cnmi = line
            return self._reply()
        if upper.startswith("AT+CSCS="):
            return self._reply()

        if upper == "AT+CPIN?":
            if not self.pin_ready:
                return self._error(cme=11)
            return self._reply(["+CPIN: READY"])
        if upper == "AT+CSQ":
            return self._reply([f"+CSQ: {self.rssi if self.radio_enabled else 99},99"])
        if upper == "AT+CESQ":
            return self._reply([f"+CESQ: 99,99,255,255,{self.rsrq},{self.rsrp}"])
        if upper == "AT+COPS?":
            if not self.registered:
                return self._reply(["+COPS: 0"])
            return self._reply([f'+COPS: 0,0,"{self.operator}",7'])
        if upper == "AT+COPS=?":
            entries = ",".join(
                f'({status},"{long_name}","{short_name}","{numeric}",{act})'
                for status, long_name, short_name, numeric, act in self.scanned_operators
            )
            return self._reply([f"+COPS: {entries}"])
        if upper == "AT+COPS=0":
            if self.radio_enabled and self.cops_recovers_registration:
                self.registered = True
            return self._reply()
        match = re.fullmatch(r'AT\+COPS=1,2,"(\d{5,6})"', upper)
        if match:
            numeric = match.group(1)
            selected = next(
                (entry for entry in self.scanned_operators if entry[3] == numeric),
                None,
            )
            if selected is None:
                return self._error(cme=30)
            self.operator_numeric = numeric
            self.operator = selected[1]
            if self.radio_enabled and self.cops_recovers_registration:
                self.registered = True
            return self._reply()
        if upper == "AT+CCED=0,1":
            return self._reply(self.cced_lines)
        if upper in ("AT+CCED=0,2", "AT+CCED=0,8"):
            # V1011 answers both neighbour dump values identically.
            return self._reply(self.cced_neighbour_lines)
        if upper == "AT+CCED":
            # Bare execute form needs parameters; V1011 rejects it with CME 3.
            return self._error(cme=3)
        if upper == "AT+EEMGINFO":
            if not self.eemginfo_supported:
                return self._error()
            return self._reply(self.eemginfo_lines)
        if upper == "AT*BANDIND?":
            return self._reply(self.bandind_lines)
        if upper == "AT^SYSINFO":
            return self._reply(self.sysinfo_lines)
        if upper in ("AT+CREG=1", "AT+CEREG=1", "AT+CIREG=1"):
            return self._reply()
        if upper in ("AT+CEREG?", "AT+CREG?"):
            if "CEREG" in upper:
                # V1011 answers the EPS query under the GPRS prefix.
                name = "+CGREG" if self.cereg_answers_as_cgreg else "+CEREG"
            else:
                name = "+CREG"
            attached = self.radio_enabled and self.registered
            return self._reply([f"{name}: 0,{1 if attached else 2}"])
        if upper == "AT+CIREG?":
            attached = self.radio_enabled and self.ims_registered
            # AirM2M_780EPV_V1011 reports notification mode 2 here.
            return self._reply([f"+CIREG: 2,{1 if attached else 0}"])
        if upper == "AT+CFUN?":
            return self._reply([f"+CFUN: {1 if self.radio_enabled else 0}"])
        if upper in ("AT+CFUN=0", "AT+CFUN=1"):
            self.radio_enabled = upper.endswith("1")
            self.registered = self.radio_enabled and self.cfun_recovers_registration
            return self._reply()
        if upper == "AT+RESET":
            self.reset_count += 1
            self.registered = self.radio_enabled and self.reset_recovers_registration
            return self._reply()
        if upper in ("AT+ICCID", "AT+CCID"):
            return self._reply([f"+ICCID: {self.iccid}"])
        if upper == "AT+CGSN":
            return self._reply([self.imei])
        if upper == "AT+CGMR":
            return self._reply([self.firmware])
        if upper == "AT+CSCA?":
            return self._reply([f'+CSCA: "{self.smsc}",145'])
        if upper == "AT+CBC":
            return self._reply(["+CBC: 0,80,4012"])
        if upper == "AT+CCLK?":
            now = datetime.now(timezone(timedelta(hours=8)))
            return self._reply([f'+CCLK: "{now:%y/%m/%d,%H:%M:%S}+32"'])

        if upper.startswith("AT+CPMS"):
            return self._handle_cpms(line)
        if upper.startswith("AT+CMGL"):
            return self._handle_cmgl(line)
        if upper.startswith("AT+CMGR"):
            return self._handle_cmgr(line)
        if upper.startswith("AT+CMGD"):
            return self._handle_cmgd(line)
        if upper.startswith("AT+CMGS"):
            return self._handle_cmgs(line)
        if upper.startswith("AT+CIPPING"):
            return self._handle_ping(line)

        self._error(cme=4)

    def _handle_cpms(self, line: str) -> None:
        used, cap = len(self._messages), self.capacity
        if line.endswith("?"):
            return self._reply(
                [f'+CPMS: "{self.storage}",{used},{cap},'
                 f'"{self.storage}",{used},{cap},'
                 f'"{self.storage}",{used},{cap}']
            )
        match = re.search(r'"(\w+)"', line)
        if match:
            self.storage = match.group(1)
        self._reply([f"+CPMS: {used},{cap},{used},{cap},{used},{cap}"])

    def _handle_cmgl(self, line: str) -> None:
        stat = 4
        if "=" in line:
            try:
                stat = int(line.split("=", 1)[1].strip() or 4)
            except ValueError:
                return self._error(cms=304)

        lines: list[str] = []
        for msg in sorted(self._messages.values(), key=lambda m: m.index):
            if stat != 4 and msg.stat != stat:
                continue
            lines.append(f"+CMGL: {msg.index},{msg.stat},,{msg.tpdu_len}")
            lines.append(msg.pdu)
            msg.stat = STAT_REC_READ
        self._reply(lines)

    def _handle_cmgr(self, line: str) -> None:
        try:
            index = int(line.split("=", 1)[1].strip())
        except (IndexError, ValueError):
            return self._error(cms=304)
        msg = self._messages.get(index)
        if msg is None:
            return self._error(cms=321)  # invalid memory index
        pdu = msg.pdu
        if self.truncate_reads > 0:
            self.truncate_reads -= 1
            pdu = pdu[:-4] or pdu
        self._reply([f"+CMGR: {msg.stat},,{msg.tpdu_len}", pdu])
        msg.stat = STAT_REC_READ

    def _handle_cmgd(self, line: str) -> None:
        try:
            args = line.split("=", 1)[1].split(",")
            index = int(args[0])
            flag = int(args[1]) if len(args) > 1 else 0
        except (IndexError, ValueError):
            return self._error(cms=304)

        if flag == 4:  # delete everything, regardless of index
            self._messages.clear()
            return self._reply()
        if flag == 1:  # all read messages
            for key in [k for k, m in self._messages.items() if m.stat == STAT_REC_READ]:
                del self._messages[key]
            return self._reply()
        if index not in self._messages:
            return self._error(cms=321)
        del self._messages[index]
        self._reply()

    def _handle_cmgs(self, line: str) -> None:
        if self._cmgf != 0:
            return self._error(cms=305)  # only PDU mode is implemented
        try:
            length = int(line.split("=", 1)[1].strip())
        except (IndexError, ValueError):
            return self._error(cms=304)
        self._pending_send = length
        self._send_buffer = ""
        self._write(f"{CRLF}> ")

    def _finish_send(self, body: str) -> None:
        expected = self._pending_send
        self._pending_send = None
        self._send_buffer = ""
        pdu = re.sub(r"\s", "", body)

        if self.fail_next_send:
            self.fail_next_send = False
            return self._error(cms=41)  # temporary failure

        try:
            decoded = codec.decode_pdu(pdu)
            actual = len(bytes.fromhex(pdu)) - 1
        except Exception:
            return self._error(cms=304)

        if expected is not None and actual != expected:
            log.warning("mock: AT+CMGS length %d but PDU carries %d", expected, actual)
            return self._error(cms=304)

        self.sent.append(decoded)
        mr = self._next_mr
        self._next_mr = (self._next_mr + 1) % 256
        self._reply([f"+CMGS: {mr}"])

    def _handle_ping(self, line: str) -> None:
        match = re.search(r'"([^"]+)"', line)
        host = match.group(1) if match else "unknown"
        self.pings.append(host)
        self._reply([f'+CIPPING: 1,"{host}",32,118,64'])


# --------------------------------------------------------------------------
# standalone runner: expose the mock on a pty for manual poking
# --------------------------------------------------------------------------


async def _run_console() -> None:
    from .at.transport import FdTransport, create_pty_pair

    pair = create_pty_pair()
    mock = MockAir780E(transport=FdTransport(pair.master_fd, "mock"))
    await mock.start()

    print(f"mock Air780E listening on: {pair.slave_path}")
    print("commands:  sms <sender> <text>   |  fill <n>  |  signal <0-31>  |  quit")
    print("try it with:  python -m air780e_agent.probe " + pair.slave_path)

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), __import__("sys").stdin
    )

    while True:
        line = (await reader.readline()).decode().strip()
        if not line:
            continue
        verb, _, rest = line.partition(" ")
        if verb in ("quit", "exit"):
            break
        if verb == "sms":
            sender, _, text = rest.partition(" ")
            ok = mock.deliver(sender or "10086", text or "test")
            print("delivered" if ok else "DROPPED — storage full")
        elif verb == "fill":
            mock.fill_storage(int(rest or 1))
            print(f"stored: {mock.stored_count}/{mock.capacity}")
        elif verb == "signal":
            mock.rssi = int(rest)
            print(f"rssi = {mock.rssi}")
        else:
            print(f"unknown command: {verb}")

    await mock.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(_run_console())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
