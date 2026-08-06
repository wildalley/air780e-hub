"""PDU layer tests.

The external vectors below are the standard worked examples from the 3GPP
literature; everything else is round-tripped through our own encoder so that
a change to packing, splitting or alignment shows up immediately.
"""

from __future__ import annotations

import time

import pytest

from air780e_agent.pdu import (
    PduError,
    Reassembler,
    alphabet_from_dcs,
    decode_pdu,
    encode_submit,
    gsm7,
)
from air780e_agent.pdu.codec import _decode_ucs2

# A textbook SMS-DELIVER: "How are you?" from +31641600986.
DELIVER_HOW_ARE_YOU = (
    "07911326040000F0040B911346610089F6"
    "0000208062917314080CC8F71D14969741F977FD07"
)


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
