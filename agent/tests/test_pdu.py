"""PDU layer tests.

The external vectors below are the standard worked examples from the 3GPP
literature; everything else is round-tripped through our own encoder so that
a change to packing, splitting or alignment shows up immediately.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from air780e_agent.pdu import (
    PduError,
    Reassembler,
    alphabet_from_dcs,
    decode_pdu,
    decode_status_report,
    encode_deliver,
    encode_status_report,
    encode_submit,
    gsm7,
    salvage,
)
from air780e_agent.pdu.codec import _decode_ucs2, _encode_address, _encode_scts

# A textbook SMS-DELIVER: "How are you?" from +31641600986.
DELIVER_HOW_ARE_YOU = (
    "07911326040000F0040B911346610089F6"
    "0000208062917314080CC8F71D14969741F977FD07"
)

# Frames captured from a real modem with octets missing between the
# originating address and the user data.  They are kept exactly as received,
# trailing zero padding and all.
#
# Each has an alphanumeric sender and a TP-SCTS that is not a date, because
# the "SCTS" is really message body: the fields the decoder reads at their
# nominal offsets are all shifted.  Decoding them to the letter of the spec
# therefore yields mojibake, and the readable middle only comes back by
# re-phasing the 7-bit stream.
#
# The giffgaff four were once read as *data* messages, on the strength of the
# invalid UDH that the shifted read reports.  Re-phasing shows what they are:
# ordinary English roaming notices, hit by the same truncation as the
# verification codes.  The UDH those tests keyed on was never a UDH.
DELIVER_TRUNCATED_KRAKEN = (
    (
        "0791448720003023240BD04B79785D7603B21B642FCBD3E6F4384C4FBFDDA0F19B5C06A5E73AD0EC"
        "46C3D9722E10F1ED3ED1417374585E06D1D1E93968FC269741F7341D0D0ABBF36F7779077AD7E5A0"
        "721BCE7EE7CBE539E89E66B341EEB2BD2C0785E76B90F9",
        "374869",
    ),
    (
        "0791448720003023240BD04B79785D7603B21B642FCBD3E6F4384C4FBFDDA0F19B5C06A5E73A90CD"
        "76C3E1622E10F1ED3ED1417374585E06D1D1E93968FC269741F7341D0D0ABBF36F7779077AD7E5A0"
        "721BCE7EE7CBE539E89E66B341EEB2BD2C0785E76B90F9",
        "667881",
    ),
)

# The same fault, but the octets that went missing took the code with them.
# What survives reads like a whole sentence — which is exactly why it must not
# be shown as one.
DELIVER_TRUNCATED_GITHUB = (
    "0791448720003023240BD0E7341D5D170350F65D97838E693AB22E0685EB7474D94D4F8FC3F4F4DB0D"
    "9A97E9753868FC26975D"
)

# Roaming notices from giffgaff, paired with a phrase from the recoverable
# middle of each.
DELIVER_TRUNCATED_GIFFGAFF = (
    (
        "0791448720003023440ED0E7B4D97C0E9BCDCF0016A81D7687CF65A01C5E7693D3EE330B34BFA7E9"
        "6334C8FDA6A7CDE971989E7EBBE7A0B7FBC0369B416F39885E97BB41F277B89D769F416FB3199476"
        "83F2EFBA1C141E8FDF75375D073AA7CD66173BFF2287E768F13B2C272B144374B92C9FBB40D3B0B9"
        "0CA2CBC3F6327BEE0200000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000",
        "turn roaming off in your account: giff.ly/dashboard",
    ),
    (
        "0791448720003023400ED0E7B4D97C0E9BCDCD00D0DB0DAACFD3EE3328FFAECB4170F4DB5D0685C5"
        "F27798CC02CDCB747ADA7D06CDE1653739ED3E83C661F89C050ABBC9203ABA0C12",
        "on using your phone abroad, setting spending caps",
    ),
    (
        "0791448720003023640ED0E7B4D97C0E9BCD59007B993D7EB7CB20A01B3444A7DD6117A8195E9741"
        "F3BABC0CCABFEB7250783C7ED7DD74507A0E4ABB416379999CA683E86F507D5E06C9DFE176DA7D06"
        "CDCB727B7A5C9E83D06579D905CABEEBA0F1BBCE2683C2ECF91B24AEE74161105D1EB697D9207298"
        "1E0685C9E4D6DB0D4ABB417474191486C341F437A83E2F83E8000000000000000000000000000000"
        "0000000000000000",
        "Make sure your account is in credit to use roaming services",
    ),
)

# The fourth giffgaff frame: 28 octets, and the modem's own TP-UDL lands on
# zero.  It was read as an empty operator control message, on a TP-PID that is
# body like everything else after the sender.  Ten octets of a roaming notice
# is not enough to recover anything worth showing.
DELIVER_TRUNCATED_GIFFGAFF_FRAGMENT = (
    "0791448720003023000ED0E7B4D97C0E9BCDDD00BA0E740E9BCD2E00"
)

_SMSC_HEX = "0791448720003023"  # +447802003032, the centre in every capture
_WHEN = datetime(2026, 8, 18, 9, 30, 15, tzinfo=timezone(timedelta(hours=1)))


def _alphanumeric_oa(name: str) -> bytes:
    """An originating-address field carrying a sender name, as a network sends."""
    septets = gsm7.encode(name)
    # The length is in semi-octets of packed 7-bit data, not in characters.
    return bytes([-(-len(septets) * 7 // 4), 0xD0]) + gsm7.pack(septets)


def _build_deliver(
    oa: bytes,
    *,
    first: int = 0x04,
    pid: int = 0x00,
    dcs: int = 0x00,
    body: bytes = b"",
    udl: int | None = None,
) -> str:
    """A well-formed SMS-DELIVER around whatever fields a test wants to vary."""
    tpdu = (
        bytes([first])
        + oa
        + bytes([pid, dcs])
        + _encode_scts(_WHEN)
        + bytes([len(body) if udl is None else udl])
        + body
    )
    return (_SMSC_HEX + tpdu.hex()).upper()


# --------------------------------------------------------------------------
# GSM 7-bit alphabet
# --------------------------------------------------------------------------


def test_gsm7_pack_unpack_roundtrip():
    septets = gsm7.encode("How are you?")
    packed = gsm7.pack(septets)
    assert gsm7.unpack(packed, len(septets)) == septets


def test_gsm7_known_packing():
    # "How are you?" packs to this exact octet string in the vector above.
    assert gsm7.pack(gsm7.encode("How are you?")).hex().upper() == (
        "C8F71D14969741F977FD07"
    )


def test_gsm7_extension_characters():
    text = "price {50} €uro [x] ~y~ |z|"
    assert gsm7.can_encode(text)
    septets = gsm7.encode(text)
    # Every extension character costs two septets.
    assert len(septets) > len(text)
    assert gsm7.decode(septets) == text


def test_gsm7_rejects_non_alphabet():
    assert not gsm7.can_encode("验证码")
    with pytest.raises(ValueError):
        gsm7.encode("验证码")


def test_gsm7_fill_bits_shift_the_stream():
    septets = gsm7.encode("ABCDEFG")
    packed = gsm7.pack(septets, fill_bits=1)
    assert gsm7.unpack(packed, len(septets), fill_bits=1) == septets


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------


def test_decode_deliver_vector():
    sms = decode_pdu(DELIVER_HOW_ARE_YOU)
    assert sms.kind == "deliver"
    assert sms.address == "+31641600986"
    assert sms.text == "How are you?"
    assert sms.smsc == "+31624000000"
    assert sms.alphabet == "gsm7"
    assert sms.concat is None
    assert not sms.is_multipart


def test_decode_deliver_timestamp():
    sms = decode_pdu(DELIVER_HOW_ARE_YOU)
    assert sms.timestamp is not None
    assert (sms.timestamp.year, sms.timestamp.month, sms.timestamp.day) == (
        2002, 8, 26,
    )
    assert (sms.timestamp.hour, sms.timestamp.minute) == (19, 37)


def test_decode_tolerates_whitespace():
    spaced = " ".join(
        DELIVER_HOW_ARE_YOU[i : i + 4] for i in range(0, len(DELIVER_HOW_ARE_YOU), 4)
    )
    assert decode_pdu(spaced).text == "How are you?"


def test_decode_rejects_garbage():
    with pytest.raises(PduError):
        decode_pdu("not hex at all")
    with pytest.raises(PduError):
        decode_pdu("0704")  # truncated


def test_alphabet_from_dcs():
    assert alphabet_from_dcs(0x00) == "gsm7"
    assert alphabet_from_dcs(0x08) == "ucs2"
    assert alphabet_from_dcs(0x04) == "8bit"
    assert alphabet_from_dcs(0xF0) == "gsm7"
    assert alphabet_from_dcs(0xF4) == "8bit"


# --------------------------------------------------------------------------
# truncated frames and best-phase recovery
# --------------------------------------------------------------------------


@pytest.mark.parametrize("pdu,code", DELIVER_TRUNCATED_KRAKEN)
def test_truncated_frame_gives_up_its_verification_code(pdu: str, code: str):
    sms = decode_pdu(pdu)

    assert sms.address == "Kraken"
    assert sms.truncated
    assert sms.code == code
    assert "verification code is: " + code in sms.recovered_text
    # The code is the point of the exercise, but the frame is still a fragment
    # and nothing may present it otherwise.
    assert sms.is_binary


def test_truncated_frame_keeps_its_spec_conformant_decode_untouched():
    # Recovery is a bypass, not a repair: what the letter of the spec made of
    # these octets stays in `text`, so a later reading of the same capture is
    # comparing like with like.
    pdu, _ = DELIVER_TRUNCATED_KRAKEN[0]
    sms = decode_pdu(pdu)

    assert sms.dcs == 0x1B  # body read as TP-DCS: UCS-2, message class 3
    assert sms.alphabet == "ucs2"
    assert sms.timestamp is None
    assert sms.text != sms.recovered_text
    assert "374869" not in sms.text


def test_truncated_frame_whose_code_was_in_the_lost_octets():
    # Reads as a complete sentence and is not one: the code sat in the head the
    # modem dropped.  Showing this as a normal body would say GitHub sent no
    # code, so it stays in the salvage field with the frame marked damaged.
    sms = decode_pdu(DELIVER_TRUNCATED_GITHUB)

    assert sms.address == "github"
    assert sms.truncated
    assert sms.code == ""
    assert sms.recovered_text == "Your GitHub authentication setup code."
    assert sms.is_binary


@pytest.mark.parametrize("pdu,phrase", DELIVER_TRUNCATED_GIFFGAFF)
def test_truncated_roaming_notices_recover_as_english_text(pdu: str, phrase: str):
    # Not data messages: English roaming notices caught by the truncation.
    sms = decode_pdu(pdu)

    assert sms.address == "giffgaff"
    assert sms.dcs == 0
    assert sms.truncated
    assert phrase in sms.recovered_text
    assert sms.code == ""  # a roaming notice carries no code to find


def test_truncated_fragment_too_short_to_recover_claims_nothing():
    # Damaged is a fact about the frame; recovered is a claim about content.
    # Ten octets support the first and not the second.
    sms = decode_pdu(DELIVER_TRUNCATED_GIFFGAFF_FRAGMENT)

    assert sms.address == "giffgaff"
    assert sms.truncated
    assert sms.recovered_text == ""
    assert sms.code == ""
    assert sms.is_binary


def test_healthy_alphanumeric_sender_is_not_touched_by_the_bypass():
    # An alphanumeric sender is half the signature and must not be enough on
    # its own, or every bank and courier message would route through salvage.
    body = "Your Kraken verification code is: 374869. Do not share it."
    septets = gsm7.encode(body)
    pdu = _build_deliver(
        _alphanumeric_oa("Kraken"), body=gsm7.pack(septets), udl=len(septets)
    )
    sms = decode_pdu(pdu)

    assert sms.address == "Kraken"
    assert sms.text == body
    assert sms.timestamp == _WHEN
    assert not sms.truncated
    assert not sms.is_binary
    # `code` belongs to the salvage path; a healthy message carries its own.
    assert sms.recovered_text == ""
    assert sms.code == ""


def test_healthy_message_with_an_unreadable_body_is_not_called_truncated():
    # Chinese scores badly on an English readability test, so nothing may hang
    # on that score alone.  Class 3 UCS-2 (TP-DCS 0x1B) is the case the
    # truncated frames imitate, and here it is genuine.
    text = "验证码 8899,请勿泄露"
    pdu = _build_deliver(
        _encode_address("+8613800138000"), dcs=0x1B, body=text.encode("utf-16-be")
    )
    sms = decode_pdu(pdu)

    assert sms.dcs == 0x1B
    assert sms.alphabet == "ucs2"
    assert sms.text == text
    assert not sms.truncated
    assert not sms.is_binary


def test_genuinely_malformed_udh_is_still_treated_as_data():
    # The signal the giffgaff frames were wrongly credited with, on a frame
    # that really has it: a well-formed header whose UDHL overruns the body.
    pdu = _build_deliver(
        _encode_address("+8613800138000"),
        first=0x04 | 0x40,  # TP-UDHI
        body=bytes([0x20]) + b"short",
        udl=6,
    )
    sms = decode_pdu(pdu)

    assert sms.udh_malformed
    assert sms.is_binary
    assert not sms.truncated


def test_empty_control_message_is_treated_as_data():
    # An operator control frame: service-centre-specific TP-PID, no user data.
    pdu = _build_deliver(_encode_address("+8613800138000"), pid=0xC0)
    sms = decode_pdu(pdu)

    assert sms.pid == 0xC0
    assert sms.text == ""
    assert sms.is_binary
    assert not sms.truncated


# --------------------------------------------------------------------------
# recovery internals
# --------------------------------------------------------------------------


def test_readability_separates_a_real_phase_from_a_wrong_one():
    # Both are printable ASCII; only one is a message.  Printability alone
    # cannot choose between them, which is why the score weighs word shape.
    assert salvage.readability("Your GitHub authentication setup code.") > 0.9
    assert salvage.readability("Plw:9Pc4:d:1Pp::t27ztq0zt77Py2z:8Pq7r2") < 0.6


def test_extract_code_ignores_digits_that_are_not_code_shaped():
    assert salvage.extract_code("your code is 374869 today") == "374869"
    assert salvage.extract_code("no digits here") == ""
    # Code-shaped digits with nothing calling them a code: a phone number
    # handed back as a verification code is a number someone would act on.
    assert salvage.extract_code("call 020 7946 0018 at 9am") == ""


def test_extract_code_prefers_the_run_nearest_the_word_code():
    text = "Ref 90210447 for order 5567: your code is 374869"
    assert salvage.extract_code(text) == "374869"
    assert salvage.extract_code("374869 is your Kraken code") == "374869"


def test_recover_declines_rather_than_invent_a_reading():
    # Random octets have a best phase like anything else; it must not be
    # dressed up as a recovery.
    assert salvage.recover(bytes(range(40))).text == ""


def test_recover_declines_on_too_few_characters_to_judge():
    # "Code 8829" would score well on a ratio, and says nothing: seven octets
    # read on the right phase look much like seven read on the wrong one.
    short = gsm7.pack(gsm7.encode("Code 8829"))
    assert salvage.recover(short).text == ""


# --------------------------------------------------------------------------
# UCS-2 byte order
# --------------------------------------------------------------------------


def test_ucs2_big_endian_without_bom():
    # The common case: UTF-16BE, no BOM.  "中文" is U+4E2D U+6587.
    payload = bytes.fromhex("4E2D6587")
    assert _decode_ucs2(payload) == "中文"


def test_ucs2_big_endian_with_bom():
    # A leading FE FF marks big-endian and must be stripped, not decoded.
    payload = b"\xfe\xff" + bytes.fromhex("4E2D6587")
    assert _decode_ucs2(payload) == "中文"


def test_ucs2_little_endian_with_bom():
    # FF FE marks little-endian: the bytes of each unit are swapped.  Decoding
    # this as big-endian is exactly the mojibake the fix exists to prevent.
    payload = b"\xff\xfe" + bytes.fromhex("2D4E8765")
    assert _decode_ucs2(payload) == "中文"
    assert _decode_ucs2(payload) != payload[2:].decode("utf-16-be")


def test_ucs2_ascii_range_still_decodes():
    # A BOM-less run of Latin text is the same either way; make sure the
    # default path handles it.
    payload = "hi".encode("utf-16-be")
    assert _decode_ucs2(payload) == "hi"


# --------------------------------------------------------------------------
# encoding round trips
# --------------------------------------------------------------------------


def _roundtrip(number: str, text: str, **kwargs):
    parts = encode_submit(number, text, **kwargs)
    decoded = [decode_pdu(p.pdu_hex) for p in parts]
    return parts, decoded


def test_encode_short_ascii():
    parts, decoded = _roundtrip("10086", "CXHF")
    assert len(parts) == 1
    assert parts[0].tpdu_len == len(bytes.fromhex(parts[0].pdu_hex)) - 1
    assert decoded[0].kind == "submit"
    assert decoded[0].address == "10086"
    assert decoded[0].text == "CXHF"
    assert decoded[0].alphabet == "gsm7"
    assert decoded[0].status_report_requested


def test_submit_can_disable_status_report_request():
    _, decoded = _roundtrip("10086", "CXHF", request_status_report=False)
    assert not decoded[0].status_report_requested


@pytest.mark.parametrize(
    "status,state",
    [(0x00, "delivered"), (0x20, "pending"), (0x40, "failed"), (0x60, "failed")],
)
def test_decode_status_report(status: int, state: str):
    pdu = encode_status_report(42, "+8613800138000", status=status)
    report = decode_status_report(pdu)

    assert report.message_reference == 42
    assert report.recipient == "+8613800138000"
    assert report.status == status
    assert report.state == state
    assert report.service_center_timestamp is not None
    assert report.discharge_time is not None
    assert report.raw == pdu


def test_status_report_decoder_rejects_a_deliver_pdu():
    with pytest.raises(PduError, match="SMS-STATUS-REPORT"):
        decode_status_report(DELIVER_HOW_ARE_YOU)


def test_encode_international_number():
    _, decoded = _roundtrip("+8613800138000", "hi")
    assert decoded[0].address == "+8613800138000"


def test_encode_chinese_uses_ucs2():
    text = "验证码 123456,请勿泄露"
    parts, decoded = _roundtrip("10086", text)
    assert len(parts) == 1
    assert decoded[0].alphabet == "ucs2"
    assert decoded[0].text == text


def test_encode_exactly_160_ascii_is_single_part():
    text = "A" * 160
    parts, decoded = _roundtrip("10086", text)
    assert len(parts) == 1
    assert decoded[0].text == text


def test_encode_161_ascii_splits():
    text = "".join(str(i % 10) for i in range(161))
    parts, _ = _roundtrip("10086", text)
    assert len(parts) == 2
    assert [p.seq for p in parts] == [1, 2]
    assert all(p.total == 2 for p in parts)


def test_encode_exactly_70_chinese_is_single_part():
    text = "测" * 70
    parts, decoded = _roundtrip("10086", text)
    assert len(parts) == 1
    assert decoded[0].text == text


def test_tpdu_len_excludes_smsc_octet():
    for part in encode_submit("10086", "x" * 300):
        raw = bytes.fromhex(part.pdu_hex)
        assert raw[0] == 0x00  # empty SMSC field
        assert part.tpdu_len == len(raw) - 1


def test_encode_never_splits_an_escape_pair():
    # Pack the segment boundary full of two-septet characters.
    text = "{" * 200
    parts = encode_submit("10086", text)
    reassembled = _reassemble(parts)
    assert reassembled == text


def test_force_ucs2():
    parts, decoded = _roundtrip("10086", "plain ascii", force_ucs2=True)
    assert decoded[0].alphabet == "ucs2"
    assert decoded[0].text == "plain ascii"


# --------------------------------------------------------------------------
# concatenation / reassembly
# --------------------------------------------------------------------------


def _reassemble(parts, timeout: float = 30.0) -> str:
    r = Reassembler(timeout=timeout)
    out = None
    for part in parts:
        got = r.push(decode_pdu(part.pdu_hex))
        if got is not None:
            out = got
    assert out is not None, "message never completed"
    return out.text


def test_long_ascii_reassembles():
    text = "".join(chr(ord("a") + i % 26) for i in range(400))
    parts = encode_submit("10086", text)
    assert len(parts) == 3
    assert _reassemble(parts) == text


def test_long_chinese_reassembles():
    text = "".join("中文短信测试" [i % 6] for i in range(200))
    parts = encode_submit("10086", text)
    assert len(parts) > 1
    assert _reassemble(parts) == text


def test_segments_carry_concat_header():
    parts = encode_submit("10086", "x" * 400)
    refs = set()
    for part in parts:
        sms = decode_pdu(part.pdu_hex)
        assert sms.concat is not None
        assert sms.is_multipart
        assert sms.concat.total == len(parts)
        refs.add(sms.concat.ref)
    assert len(refs) == 1, "all segments must share one reference"


def test_reassembly_out_of_order():
    text = "y" * 400
    parts = encode_submit("10086", text)
    shuffled = list(reversed(parts))
    assert _reassemble(shuffled) == text


def test_reassembler_passes_single_part_straight_through():
    r = Reassembler()
    sms = decode_pdu(DELIVER_HOW_ARE_YOU)
    assert r.push(sms) is sms
    assert r.pending_count == 0


def test_reassembler_keeps_senders_apart():
    r = Reassembler()
    a = encode_submit("10086", "a" * 400, ref=7)
    b = encode_submit("10010", "b" * 400, ref=7)  # same reference, other sender
    for part in a[:-1] + b[:-1]:
        assert r.push(decode_pdu(part.pdu_hex)) is None
    assert r.pending_count == 2
    assert r.push(decode_pdu(a[-1].pdu_hex)) is not None
    assert r.push(decode_pdu(b[-1].pdu_hex)) is not None
    assert r.pending_count == 0


def test_reassembler_flushes_expired_partials():
    r = Reassembler(timeout=0.01)
    parts = encode_submit("10086", "z" * 400)
    assert r.push(decode_pdu(parts[0].pdu_hex)) is None
    assert r.pending_count == 1
    assert r.flush_expired() == []  # not yet due
    time.sleep(0.02)
    flushed = r.flush_expired()
    assert len(flushed) == 1
    assert flushed[0].text  # partial content survives rather than being lost
    assert r.pending_count == 0


# --------------------------------------------------------------------------
# binary / port-addressed SMS
# --------------------------------------------------------------------------


def _deliver_with_udh(udh: bytes, payload: bytes, dcs: int) -> str:
    """Hand-build one SMS-DELIVER carrying *udh* + *payload*.

    Written out rather than reusing ``encode_deliver`` because that only builds
    text messages, and the point here is a PDU whose user data was never text.
    TP-UDL is in octets because every case below uses an 8-bit DCS.
    """
    first = 0x00 | (0x40 if udh else 0x00)   # TP-MTI=DELIVER, TP-UDHI when a UDH
    body = (bytes([len(udh)]) + udh if udh else b"") + payload
    return "".join([
        "00",                       # no SMSC in the returned PDU
        f"{first:02X}",
        "05", "81", "0100F8",       # TP-OA: 5 digits, national, "10086"
        "00",                       # TP-PID
        f"{dcs:02X}",               # TP-DCS
        "62808081204300",           # TP-SCTS
        f"{len(body):02X}",         # TP-UDL
        body.hex().upper(),
    ])


def test_port_addressed_sms_is_flagged_as_binary():
    """An 8-bit port-addressing UDH means the payload is for an application.

    Operators push OTA/config messages this way. Decoded as text they become a
    wall of mojibake in a conversation, so the decoder has to say what they are
    — before this, ``_parse_udh`` only looked for concatenation and such a
    message was indistinguishable from one a person sent.
    """
    udh = bytes([0x04, 0x02, 0x0B, 0x84])       # IEI 0x04, dest 0x0B, src 0x84
    sms = decode_pdu(_deliver_with_udh(udh, bytes(range(16)), dcs=0x04))

    assert sms.ports == (0x0B, 0x84)
    assert sms.is_binary


def test_sixteen_bit_port_addressing_is_also_flagged():
    udh = bytes([0x05, 0x04, 0x0B, 0x84, 0x23, 0xF0])
    sms = decode_pdu(_deliver_with_udh(udh, b"\x01\x02\x03", dcs=0x04))

    assert sms.ports == (0x0B84, 0x23F0)
    assert sms.is_binary


def test_eight_bit_dcs_alone_is_enough_to_flag_binary():
    """No UDH at all, just a data coding scheme that says octets."""
    sms = decode_pdu(_deliver_with_udh(b"", b"\xde\xad\xbe\xef", dcs=0x04))

    assert sms.alphabet == "8bit"
    assert sms.ports is None
    assert sms.is_binary


def test_a_udh_carrying_both_concat_and_ports_reports_both():
    """A real UDH often holds several IEs; the walk must not stop at the first.

    Before this the loop returned as soon as it found concatenation, so a
    multipart OTA message read as ordinary text.
    """
    udh = bytes([0x00, 0x03, 0x2A, 0x02, 0x01]) + bytes([0x04, 0x02, 0x0B, 0x84])
    sms = decode_pdu(_deliver_with_udh(udh, bytes(8), dcs=0x04))

    assert sms.concat is not None
    assert (sms.concat.ref, sms.concat.total, sms.concat.seq) == (0x2A, 2, 1)
    assert sms.ports == (0x0B, 0x84)
    assert sms.is_binary


def test_a_concatenated_text_message_is_not_binary():
    """The flag must not fire on the UDH that ordinary long messages carry."""
    for part in encode_submit("10086", "x" * 400):
        sms = decode_pdu(part.pdu_hex)
        assert sms.concat is not None
        assert sms.ports is None
        assert not sms.is_binary


def test_a_plain_text_message_is_not_binary():
    sms = decode_pdu(encode_deliver("10086", "验证码 123456")[0])
    assert not sms.is_binary
    assert sms.ports is None
