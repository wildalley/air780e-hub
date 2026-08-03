"""GSM 03.38 / 3GPP TS 23.038 default alphabet and 7-bit packing.

The default alphabet is a 128-entry table; ten further characters live in an
extension table reached by prefixing ESC (0x1B).  Septets are packed into
octets LSB-first, which is why the packing helpers work on a bit cursor rather
than on byte boundaries.
"""

from __future__ import annotations

ESC = 0x1B

# 8 rows of 16 characters.  Index in this string == septet value.
BASIC_TABLE = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNO"
    "PQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmno"
    "pqrstuvwxyzäöñüà"
)
assert len(BASIC_TABLE) == 128

# Reached as ESC + value.
EXT_TABLE = {
    0x0A: "\f",
    0x14: "^",
    0x28: "{",
    0x29: "}",
    0x2F: "\\",
    0x3C: "[",
    0x3D: "~",
    0x3E: "]",
    0x40: "|",
    0x65: "€",
}

_BASIC_REVERSE = {c: i for i, c in enumerate(BASIC_TABLE)}
# ESC itself is not a writable character.
del _BASIC_REVERSE["\x1b"]
_EXT_REVERSE = {c: v for v, c in EXT_TABLE.items()}


def can_encode(text: str) -> bool:
    """True if every character fits the default alphabet (basic or extension)."""
    return all(c in _BASIC_REVERSE or c in _EXT_REVERSE for c in text)


def encode(text: str) -> list[int]:
    """Text -> septet values.  Raises ValueError on unrepresentable input."""
    out: list[int] = []
    for c in text:
        if c in _BASIC_REVERSE:
            out.append(_BASIC_REVERSE[c])
        elif c in _EXT_REVERSE:
            out.append(ESC)
            out.append(_EXT_REVERSE[c])
        else:
            raise ValueError(f"character {c!r} is not in the GSM 7-bit alphabet")
    return out


def decode(septets: list[int]) -> str:
    """Septet values -> text.  Unknown extension escapes fall back to space."""
    out: list[str] = []
    pending_escape = False
    for s in septets:
        if pending_escape:
            pending_escape = False
            out.append(EXT_TABLE.get(s, BASIC_TABLE[s] if s < 128 else " "))
            continue
        if s == ESC:
            pending_escape = True
            continue
        if s < 128:
            out.append(BASIC_TABLE[s])
    if pending_escape:
        # Trailing ESC with nothing after it; the spec says treat as space.
        out.append(" ")
    return "".join(out)


def pack(septets: list[int], fill_bits: int = 0) -> bytes:
    """Pack septets into octets, LSB-first.

    ``fill_bits`` shifts the whole stream up, which is how a UDH-prefixed
    7-bit payload gets realigned onto a septet boundary.
    """
    total_bits = fill_bits + 7 * len(septets)
    nbytes = (total_bits + 7) // 8
    buf = bytearray(nbytes)
    bitpos = fill_bits
    for s in septets:
        s &= 0x7F
        index, shift = divmod(bitpos, 8)
        buf[index] |= (s << shift) & 0xFF
        if shift > 1 and index + 1 < nbytes:
            buf[index + 1] |= s >> (8 - shift)
        bitpos += 7
    return bytes(buf)


def unpack(octets: bytes, septet_count: int, fill_bits: int = 0) -> list[int]:
    """Inverse of :func:`pack`.

    ``septet_count`` is needed because the final octet may contain padding
    that is indistinguishable from a real ``@`` (septet 0).
    """
    out: list[int] = []
    bitpos = fill_bits
    for _ in range(septet_count):
        index, shift = divmod(bitpos, 8)
        if index >= len(octets):
            break
        value = octets[index] >> shift
        if shift > 1 and index + 1 < len(octets):
            value |= octets[index + 1] << (8 - shift)
        out.append(value & 0x7F)
        bitpos += 7
    return out
