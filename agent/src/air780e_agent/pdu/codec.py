"""SMS PDU encoding and decoding (3GPP TS 23.040).

Covers what the Air780E actually hands us in PDU mode:

* ``SMS-DELIVER``  — incoming messages from ``+CMT``/``+CMGR``/``+CMGL``
* ``SMS-SUBMIT``   — outgoing messages for ``AT+CMGS``, and stored drafts/sent
  items that ``+CMGL`` can also return
* ``SMS-STATUS-REPORT`` — network delivery receipts from ``+CDS``

Text mode is deliberately not used anywhere in this project: it mangles
non-ASCII content and gives no access to the concatenation headers.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import gsm7, salvage

# Payload ceilings per segment, from TS 23.040 §9.2.3.24.
MAX_GSM7_SINGLE = 160
MAX_GSM7_CONCAT = 153
MAX_UCS2_SINGLE_BYTES = 140
MAX_UCS2_CONCAT_BYTES = 134

# TP-MTI values in the first TPDU octet.
MTI_DELIVER = 0
MTI_SUBMIT = 1
MTI_STATUS_REPORT = 2

_TOA_INTERNATIONAL = 0x91
_TOA_NATIONAL = 0x81
_TOA_ALPHANUMERIC = 0xD0


class PduError(ValueError):
    """Raised when a PDU is malformed or truncated."""


@dataclass(frozen=True)
class Concat:
    """Concatenation header extracted from the UDH."""

    ref: int
    total: int
    seq: int


@dataclass
class DecodedSms:
    kind: str  # "deliver" | "submit"
    address: str  # sender for deliver, recipient for submit
    text: str
    smsc: str | None = None
    timestamp: datetime | None = None
    dcs: int = 0
    alphabet: str = "gsm7"
    concat: Concat | None = None
    raw: str = ""
    #: Destination/source ports from a port-addressing UDH, when present.
    ports: tuple[int, int] | None = None
    #: The declared UDH or one of its information elements overran available data.
    udh_malformed: bool = False
    #: TP-SRR on SMS-SUBMIT; asks the service centre for a delivery report.
    status_report_requested: bool = False
    #: TP-PID, retained so operator-specific empty control messages stay data.
    pid: int = 0
    #: The frame reached us with octets missing, so ``text`` is not the
    #: message: the header fields it was decoded under are not those fields.
    truncated: bool = False
    #: Best-effort re-phasing of a truncated frame — a fragment of the middle
    #: of the message, never the whole of it.  Empty when nothing recovered
    #: was worth showing.  See :mod:`.salvage`.
    recovered_text: str = ""
    #: Code-shaped digits found in ``recovered_text``.  Empty on a truncated
    #: frame means "no code survived", which is not the same as "no code sent"
    #: — the head this decoder cannot recover is where a code usually sits.
    code: str = ""

    @property
    def is_multipart(self) -> bool:
        return self.concat is not None and self.concat.total > 1

    @property
    def is_binary(self) -> bool:
        """True when ``text`` must not be shown to a person as it stands.

        Any of these independent signals is enough:

        * an 8-bit TP-DCS — the payload is octets, not characters;
        * a port-addressing UDH — the content is addressed to an application
          (OTA provisioning, WAP push, SIM toolkit), not to the inbox;
        * a structurally invalid UDH — its payload boundary cannot be trusted,
          so rendering the remaining octets as text only produces mojibake;
        * a truncated frame — ``text`` was decoded under header fields that
          are really message body, so it is mojibake for the same reason.
          What is readable of such a message is in ``recovered_text``, and it
          is a fragment;
        * an empty service-centre-specific TP-PID message — an operator control
          frame with no text to show or forward.

        The name is older than the last two entries and undersells it: what
        these share is not that the payload is data, but that decoding it as
        text produced something no reader should be handed.  ``text`` is still
        whatever the decode produced; the caller decides whether to show it.
        """
        return (
            self.alphabet == "8bit"
            or self.ports is not None
            or self.udh_malformed
            or self.truncated
            or (not self.text and self.pid >= 0xC0)
        )


@dataclass
class EncodedPdu:
    """One segment ready for ``AT+CMGS=<tpdu_len>`` then ``<pdu_hex><Ctrl-Z>``."""

    pdu_hex: str
    tpdu_len: int
    seq: int = 1
    total: int = 1


@dataclass(frozen=True)
class StatusReport:
    """The mandatory fields of an SMS-STATUS-REPORT TPDU."""

    message_reference: int
    recipient: str
    service_center_timestamp: datetime | None
    discharge_time: datetime | None
    status: int
    smsc: str | None = None
    raw: str = ""

    @property
    def state(self) -> str:
        """Aggregate-friendly state from TP-ST (TS 23.040 section 9.2.3.15)."""
        if 0x00 <= self.status <= 0x1F:
            return "delivered"
        if 0x20 <= self.status <= 0x3F:
            return "pending"  # temporary error; the service centre is retrying
        if 0x40 <= self.status <= 0x7F:
            return "failed"
        return "pending"  # reserved values must not become a false failure


# --------------------------------------------------------------------------
# low-level helpers
# --------------------------------------------------------------------------


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def take(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise PduError(
                f"PDU truncated: wanted {n} octets at offset {self._pos}, "
                f"only {len(self._data) - self._pos} left"
            )
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def byte(self) -> int:
        return self.take(1)[0]

    def tell(self) -> int:
        return self._pos

    def rest(self) -> bytes:
        chunk = self._data[self._pos :]
        self._pos = len(self._data)
        return chunk


def _swap_decimal(octet: int) -> int:
    """Decode one semi-octet-swapped BCD byte (0x21 -> 12)."""
    return (octet & 0x0F) * 10 + (octet >> 4)


def _decode_digits(data: bytes, digit_count: int) -> str:
    out: list[str] = []
    for octet in data:
        out.append(f"{octet & 0x0F:X}")
        out.append(f"{octet >> 4:X}")
    return "".join(out[:digit_count]).replace("F", "")


def _encode_digits(digits: str) -> bytes:
    if len(digits) % 2:
        digits += "F"
    out = bytearray()
    for i in range(0, len(digits), 2):
        lo = int(digits[i], 16)
        hi = int(digits[i + 1], 16)
        out.append((hi << 4) | lo)
    return bytes(out)


def alphabet_from_dcs(dcs: int) -> str:
    """Map TP-DCS to ``gsm7`` / ``8bit`` / ``ucs2`` (TS 23.038 §4)."""
    if (dcs & 0xC0) == 0x00:
        coding = (dcs >> 2) & 0x03
    elif (dcs & 0xF0) == 0xF0:
        coding = 1 if (dcs & 0x04) else 0
    else:
        # Reserved / message-waiting groups: the sensible fallback is GSM 7-bit,
        # except for the UCS2 waiting-indication group 0x1110.
        coding = 2 if (dcs & 0xF0) == 0xE0 else 0
    return {0: "gsm7", 1: "8bit", 2: "ucs2"}.get(coding, "gsm7")


def _decode_address(reader: _Reader) -> tuple[str, int]:
    """Decode an address field, returning it with its type-of-address octet.

    The caller needs the TOA as well as the text: an alphanumeric address is
    the one place a sender name can appear, and that is half the signature of
    the truncation this decoder has to recognise.
    """
    length = reader.byte()  # in semi-octets (digits), not bytes
    if length == 0:
        return "", 0
    toa = reader.byte()
    octets = reader.take((length + 1) // 2)
    if (toa & 0x70) == 0x50:
        # Alphanumeric address (e.g. a bank's short name) packed as GSM 7-bit.
        septet_count = (length * 4) // 7
        return gsm7.decode(gsm7.unpack(octets, septet_count)), toa
    digits = _decode_digits(octets, length)
    return (("+" + digits) if (toa & 0x70) == 0x10 else digits), toa


def _is_alphanumeric(toa: int) -> bool:
    return (toa & 0x70) == 0x50


def _encode_address(number: str) -> bytes:
    international = number.strip().startswith("+")
    digits = re.sub(r"\D", "", number)
    if not digits:
        raise PduError(f"no digits in destination address {number!r}")
    toa = _TOA_INTERNATIONAL if international else _TOA_NATIONAL
    return bytes([len(digits), toa]) + _encode_digits(digits)


def _decode_smsc(reader: _Reader) -> str | None:
    sca_len = reader.byte()
    if not sca_len:
        return None
    sca = reader.take(sca_len)
    toa = sca[0]
    digits = _decode_digits(sca[1:], (sca_len - 1) * 2)
    return ("+" + digits) if (toa & 0x70) == 0x10 else digits


def _decode_scts(data: bytes) -> datetime | None:
    """Decode the 7-octet service-centre timestamp."""
    try:
        year = _swap_decimal(data[0])
        month = _swap_decimal(data[1])
        day = _swap_decimal(data[2])
        hour = _swap_decimal(data[3])
        minute = _swap_decimal(data[4])
        second = _swap_decimal(data[5])

        # Timezone is in quarter-hours; bit 3 of the low semi-octet is the sign.
        tz_raw = data[6]
        low = tz_raw & 0x0F
        negative = bool(low & 0x08)
        quarters = (low & 0x07) * 10 + (tz_raw >> 4)
        offset = timedelta(minutes=15 * quarters)
        if negative:
            offset = -offset

        return datetime(
            2000 + year, month, day, hour, minute, second,
            tzinfo=timezone(offset),
        )
    except (ValueError, IndexError):
        # A bad timestamp must not cost us the message body.
        return None


def _parse_udh(udh: bytes) -> tuple[Concat | None, tuple[int, int] | None, bool]:
    """Pull the concatenation and port-addressing IEs out of a user-data header.

    Both are returned because they answer different questions and either can
    appear without the other: concatenation says how to reassemble, ports say
    the payload was never meant for a human reader.  The loop keeps walking
    after a match — a real UDH often carries both IEs.  The final boolean says
    whether an element crossed the boundary declared by TP-UDHL.
    """
    concat: Concat | None = None
    ports: tuple[int, int] | None = None
    pos = 0
    while pos < len(udh):
        if pos + 2 > len(udh):
            return concat, ports, True
        iei = udh[pos]
        ie_len = udh[pos + 1]
        end = pos + 2 + ie_len
        if end > len(udh):
            return concat, ports, True
        payload = udh[pos + 2 : end]
        if iei == 0x00 and len(payload) >= 3:  # 8-bit reference
            concat = concat or Concat(ref=payload[0], total=payload[1], seq=payload[2])
        elif iei == 0x08 and len(payload) >= 4:  # 16-bit reference
            concat = concat or Concat(
                ref=(payload[0] << 8) | payload[1],
                total=payload[2],
                seq=payload[3],
            )
        elif iei == 0x04 and len(payload) >= 2:  # 8-bit port addressing
            ports = ports or (payload[0], payload[1])
        elif iei == 0x05 and len(payload) >= 4:  # 16-bit port addressing
            ports = ports or (
                (payload[0] << 8) | payload[1],
                (payload[2] << 8) | payload[3],
            )
        pos = end
    return concat, ports, False


def _decode_ucs2(payload: bytes) -> str:
    """UTF-16 with byte order taken from a leading BOM when one is present.

    Nearly every network sends UCS-2 big-endian with no BOM, which is the
    default here.  A few prepend U+FEFF, and a rare sender emits little-endian
    marked by the byte-swapped U+FFFE; honouring both costs nothing and avoids
    turning a whole message into CJK mojibake when a sender does the unusual
    thing.  This is *not* a fix for GSM 7-bit content mislabelled as UCS-2 by a
    bad TP-DCS — no byte-order choice recovers that; see ``alphabet_from_dcs``.
    """
    if payload[:2] == b"\xfe\xff":
        return payload[2:].decode("utf-16-be", errors="replace")
    if payload[:2] == b"\xff\xfe":
        return payload[2:].decode("utf-16-le", errors="replace")
    return payload.decode("utf-16-be", errors="replace")


def _decode_user_data(
    body: bytes, udl: int, dcs: int, has_udh: bool
) -> tuple[str, Concat | None, str, tuple[int, int] | None, bool]:
    alphabet = alphabet_from_dcs(dcs)
    concat: Concat | None = None
    ports: tuple[int, int] | None = None
    udh_malformed = False
    udh_octets = 0

    if has_udh:
        if not body:
            raise PduError("UDHI set but user data is empty")
        udhl = body[0]
        udh_octets = udhl + 1
        udh_malformed = udh_octets > len(body)
        concat, ports, invalid_elements = _parse_udh(body[1:udh_octets])
        udh_malformed = udh_malformed or invalid_elements

    if alphabet == "gsm7":
        # The 7-bit stream is realigned so it starts on a septet boundary.
        fill_bits = (7 - (udh_octets * 8) % 7) % 7
        header_septets = (udh_octets * 8 + fill_bits) // 7
        septets = gsm7.unpack(body[udh_octets:], udl - header_septets, fill_bits)
        text = gsm7.decode(septets)
    elif alphabet == "ucs2":
        payload = body[udh_octets:udl] if udl <= len(body) else body[udh_octets:]
        text = _decode_ucs2(payload)
    else:
        payload = body[udh_octets:udl] if udl <= len(body) else body[udh_octets:]
        text = payload.decode("latin-1", errors="replace")

    return text, concat, alphabet, ports, udh_malformed


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------


def decode_pdu(pdu_hex: str) -> DecodedSms:
    """Decode one hex PDU as returned by ``+CMGR`` / ``+CMGL`` / ``+CMT``."""
    cleaned = re.sub(r"\s", "", pdu_hex)
    try:
        data = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise PduError(f"not valid hex: {exc}") from exc

    reader = _Reader(data)

    smsc = _decode_smsc(reader)

    first = reader.byte()
    mti = first & 0x03
    has_udh = bool(first & 0x40)
    status_report_requested = False

    if mti == MTI_DELIVER:
        address, toa = _decode_address(reader)
        # Everything from here on is suspect if the frame turns out truncated:
        # remember where the header claims to start.
        body_start = reader.tell()
        pid = reader.byte()
        dcs = reader.byte()
        timestamp = _decode_scts(reader.take(7))
        kind = "deliver"
    elif mti == MTI_SUBMIT:
        status_report_requested = bool(first & 0x20)
        reader.byte()  # TP-MR
        address, toa = _decode_address(reader)
        body_start = None
        pid = reader.byte()
        dcs = reader.byte()
        vpf = (first >> 3) & 0x03
        if vpf == 0x02:  # relative
            reader.byte()
        elif vpf in (0x01, 0x03):  # enhanced / absolute
            reader.take(7)
        timestamp = None
        kind = "submit"
    else:
        raise PduError(f"unsupported TP-MTI {mti} (first octet 0x{first:02X})")

    udl = reader.byte()
    text, concat, alphabet, ports, udh_malformed = _decode_user_data(
        reader.rest(), udl, dcs, has_udh
    )

    # A deliver whose TP-SCTS is not a real date reached us damaged: the field
    # is mandatory and fixed-width, so a conformant network cannot produce one
    # that fails to parse.  Every such frame we have captured also had an
    # alphanumeric sender, and no numeric-sender frame has ever shown the
    # fault, so the bypass stays behind both signals rather than the one.
    # Nothing above this line is reconsidered — ``text`` keeps whatever the
    # spec-conformant decode made of it, and a healthy PDU cannot get here.
    truncated = kind == "deliver" and _is_alphanumeric(toa) and timestamp is None
    recovered = salvage.recover(data[body_start:]) if truncated else salvage.Salvage()

    return DecodedSms(
        kind=kind,
        address=address,
        text=text,
        smsc=smsc,
        timestamp=timestamp,
        dcs=dcs,
        alphabet=alphabet,
        concat=concat,
        raw=cleaned.upper(),
        ports=ports,
        udh_malformed=udh_malformed,
        status_report_requested=status_report_requested,
        pid=pid,
        truncated=truncated,
        recovered_text=recovered.text,
        code=recovered.code,
    )


def decode_status_report(pdu_hex: str) -> StatusReport:
    """Decode an SMS-STATUS-REPORT PDU carried by a ``+CDS`` URC."""
    cleaned = re.sub(r"\s", "", pdu_hex)
    try:
        data = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise PduError(f"not valid hex: {exc}") from exc

    reader = _Reader(data)
    smsc = _decode_smsc(reader)
    first = reader.byte()
    if (first & 0x03) != MTI_STATUS_REPORT:
        raise PduError(
            f"expected SMS-STATUS-REPORT, got TP-MTI {first & 0x03} "
            f"(first octet 0x{first:02X})"
        )
    message_reference = reader.byte()
    recipient, _ = _decode_address(reader)
    service_center_timestamp = _decode_scts(reader.take(7))
    discharge_time = _decode_scts(reader.take(7))
    status = reader.byte()
    return StatusReport(
        message_reference=message_reference,
        recipient=recipient,
        service_center_timestamp=service_center_timestamp,
        discharge_time=discharge_time,
        status=status,
        smsc=smsc,
        raw=cleaned.upper(),
    )


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------


def _split_gsm7(septets: list[int], limit: int) -> list[list[int]]:
    chunks: list[list[int]] = []
    pos = 0
    while pos < len(septets):
        end = min(pos + limit, len(septets))
        # Never end a segment on a dangling ESC — it would eat the next
        # segment's first character.
        if end < len(septets) and septets[end - 1] == gsm7.ESC:
            end -= 1
        chunks.append(septets[pos:end])
        pos = end
    return chunks


def _split_ucs2(text: str, limit_bytes: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for ch in text:
        size = len(ch.encode("utf-16-be"))  # 2, or 4 for astral chars
        if used + size > limit_bytes:
            chunks.append("".join(current))
            current, used = [], 0
        current.append(ch)
        used += size
    if current:
        chunks.append("".join(current))
    return chunks


def _build_udh(ref: int, total: int, seq: int) -> bytes:
    # 8-bit reference concatenation IE: UDHL, IEI, IEDL, ref, total, seq.
    return bytes([0x05, 0x00, 0x03, ref & 0xFF, total, seq])


def encode_submit(
    number: str,
    text: str,
    *,
    ref: int | None = None,
    validity_period: int = 0xAA,
    force_ucs2: bool = False,
    request_status_report: bool = True,
) -> list[EncodedPdu]:
    """Build the ``SMS-SUBMIT`` segments for one outgoing message.

    Returns one :class:`EncodedPdu` per segment; a long message yields several,
    each carrying a concatenation header so the handset reassembles them.
    The SMSC field is left empty (``00``) so the modem uses the number already
    provisioned on the SIM.
    """
    use_gsm7 = (not force_ucs2) and gsm7.can_encode(text)
    if ref is None:
        ref = random.randint(0, 255)

    addr = _encode_address(number)
    dcs = 0x00 if use_gsm7 else 0x08

    if use_gsm7:
        septets = gsm7.encode(text)
        multipart = len(septets) > MAX_GSM7_SINGLE
        limit = MAX_GSM7_CONCAT if multipart else MAX_GSM7_SINGLE
        segments: list[list[int] | str] = list(_split_gsm7(septets, limit))
    else:
        encoded_len = len(text.encode("utf-16-be"))
        multipart = encoded_len > MAX_UCS2_SINGLE_BYTES
        limit = MAX_UCS2_CONCAT_BYTES if multipart else MAX_UCS2_SINGLE_BYTES
        segments = list(_split_ucs2(text, limit))

    if not segments:
        segments = [[] if use_gsm7 else ""]

    total = len(segments)
    out: list[EncodedPdu] = []

    for index, segment in enumerate(segments, start=1):
        udh = _build_udh(ref, total, index) if total > 1 else b""

        first_octet = 0x01 | 0x10  # SUBMIT + relative validity period
        if request_status_report:
            first_octet |= 0x20  # TP-SRR
        if udh:
            first_octet |= 0x40  # UDHI

        if use_gsm7:
            assert isinstance(segment, list)
            fill_bits = (7 - (len(udh) * 8) % 7) % 7
            header_septets = (len(udh) * 8 + fill_bits) // 7
            body = udh + gsm7.pack(segment, fill_bits)
            udl = header_septets + len(segment)
        else:
            assert isinstance(segment, str)
            payload = segment.encode("utf-16-be")
            body = udh + payload
            udl = len(body)

        tpdu = bytes(
            [
                first_octet,
                0x00,  # TP-MR
            ]
        ) + addr + bytes(
            [
                0x00,  # TP-PID
                dcs,
                validity_period,
                udl,
            ]
        ) + body

        out.append(
            EncodedPdu(
                pdu_hex=("00" + tpdu.hex()).upper(),
                tpdu_len=len(tpdu),
                seq=index,
                total=total,
            )
        )

    return out


def _encode_scts(when: datetime) -> bytes:
    def swap(value: int) -> int:
        return ((value % 10) << 4) | (value // 10)

    offset = when.utcoffset() or timedelta(0)
    quarters = int(offset.total_seconds() // 900)
    negative = quarters < 0
    quarters = abs(quarters)
    tz_octet = ((quarters % 10) << 4) | (quarters // 10)
    if negative:
        tz_octet |= 0x08

    return bytes(
        [
            swap(when.year % 100),
            swap(when.month),
            swap(when.day),
            swap(when.hour),
            swap(when.minute),
            swap(when.second),
            tz_octet,
        ]
    )


def encode_status_report(
    message_reference: int,
    recipient: str,
    *,
    status: int = 0,
    submitted_at: datetime | None = None,
    discharged_at: datetime | None = None,
    smsc: str = "+8613800210500",
) -> str:
    """Build a network delivery report for the mock modem and tests."""
    now = datetime.now(timezone(timedelta(hours=8)))
    submitted_at = submitted_at or now
    discharged_at = discharged_at or now
    sca_digits = re.sub(r"\D", "", smsc)
    sca = bytes([0x91]) + _encode_digits(sca_digits)
    sca_field = bytes([len(sca)]) + sca
    tpdu = (
        bytes([MTI_STATUS_REPORT, message_reference & 0xFF])
        + _encode_address(recipient)
        + _encode_scts(submitted_at)
        + _encode_scts(discharged_at)
        + bytes([status & 0xFF])
    )
    return (sca_field + tpdu).hex().upper()


def encode_deliver(
    sender: str,
    text: str,
    *,
    when: datetime | None = None,
    smsc: str = "+8613800210500",
    ref: int | None = None,
    force_ucs2: bool = False,
) -> list[str]:
    """Build ``SMS-DELIVER`` PDUs as a network would send them.

    Production code never needs this — the modem hands us delivers already
    formed.  It exists so the mock modem and the tests can generate realistic
    incoming traffic, including multipart messages.
    """
    use_gsm7 = (not force_ucs2) and gsm7.can_encode(text)
    if ref is None:
        ref = random.randint(0, 255)
    if when is None:
        when = datetime.now(timezone(timedelta(hours=8)))

    sca_digits = re.sub(r"\D", "", smsc)
    sca = bytes([0x91]) + _encode_digits(sca_digits)
    sca_field = bytes([len(sca)]) + sca

    oa = _encode_address(sender)
    dcs = 0x00 if use_gsm7 else 0x08
    scts = _encode_scts(when)

    if use_gsm7:
        septets = gsm7.encode(text)
        multipart = len(septets) > MAX_GSM7_SINGLE
        limit = MAX_GSM7_CONCAT if multipart else MAX_GSM7_SINGLE
        segments: list[list[int] | str] = list(_split_gsm7(septets, limit))
    else:
        multipart = len(text.encode("utf-16-be")) > MAX_UCS2_SINGLE_BYTES
        limit = MAX_UCS2_CONCAT_BYTES if multipart else MAX_UCS2_SINGLE_BYTES
        segments = list(_split_ucs2(text, limit))

    if not segments:
        segments = [[] if use_gsm7 else ""]

    total = len(segments)
    out: list[str] = []

    for index, segment in enumerate(segments, start=1):
        udh = _build_udh(ref, total, index) if total > 1 else b""
        first_octet = 0x04 | (0x40 if udh else 0x00)  # MTI=DELIVER, MMS set

        if use_gsm7:
            assert isinstance(segment, list)
            fill_bits = (7 - (len(udh) * 8) % 7) % 7
            header_septets = (len(udh) * 8 + fill_bits) // 7
            body = udh + gsm7.pack(segment, fill_bits)
            udl = header_septets + len(segment)
        else:
            assert isinstance(segment, str)
            body = udh + segment.encode("utf-16-be")
            udl = len(body)

        tpdu = (
            bytes([first_octet])
            + oa
            + bytes([0x00, dcs])
            + scts
            + bytes([udl])
            + body
        )
        out.append((sca_field + tpdu).hex().upper())

    return out
