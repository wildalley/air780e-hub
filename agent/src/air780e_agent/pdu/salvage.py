"""Best-effort recovery of SMS frames the modem hands us already damaged.

Nothing in this module is 3GPP-conformant, and that is the point: it exists
only for frames whose mandatory header fields provably did not survive the
trip out of the modem, where conformant parsing has already failed.  It is
kept apart from :mod:`.codec` so the normal decode path cannot reach it by
accident — a healthy PDU never touches a line of this file.

The damage this repairs
=======================

Some senders arrive with a chunk of octets missing between the originating
address and the user data.  The service-centre timestamp, TP-PID, TP-DCS and
TP-UDL we read at their nominal offsets are then not those fields at all —
they are message body, and everything after them is a 7-bit stream whose
septet grid no longer lines up with the octet grid.  Decoding that stream
under the fields we *think* we read produces a wall of mojibake.

Re-phasing recovers the middle of the message.  It cannot recover the head
and tail: those octets are gone before we ever see the frame, so anything
this module returns is a fragment, and callers are told so through
``DecodedSms.truncated``.  A recovered fragment is never presented as a whole
message — an SMS whose code sat in the lost head must read as "damaged, code
unrecoverable", never as "the sender did not send a code".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import gsm7

# A GSM 7-bit stream has exactly seven distinct alignments against the octet
# grid.  Offsets of 7 bits and up repeat those seven, one leading character
# poorer, so scanning 0..6 covers the whole space.
_PHASES = range(7)

# Characters an English SMS is built from.  Everything outside this set — the
# Greek capitals and accented vowels that fill the GSM alphabet's upper half —
# is the signature of a misread stream.
_PLAIN = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " .,:;!?'\"()/+-\n"
)
_WORD = re.compile(r"[A-Za-z]+")
_VOWEL = re.compile(r"[aeiouAEIOU]")
_DIGIT_RUN = re.compile(r"\d{2,}")

# Score below which we decline to claim a recovery.  Calibrated on captured
# traffic: genuine recoveries score 0.85 and up, the best fragment we judged
# too short to be worth anything scores 0.65, and a wrong phase of a real
# message scores 0.55 or less.  0.75 sits in the empty band between.
_MIN_SCORE = 0.75

# Shorter than this and the score means nothing: it is a ratio, so on a
# handful of characters one accidentally word-shaped run carries it, and
# random octets clear the bar about as often as a real fragment does.  A code
# with enough context to be trusted does not fit in less than this anyway.
_MIN_CHARS = 20

# Digit runs this long are code-shaped.  Shorter runs are prices, dates and
# times; longer ones are account and phone numbers.
_CODE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
_CODE_CONTEXT = re.compile(r"(?:code|pin|otp|password|passcode|verification)", re.I)


@dataclass(frozen=True)
class Salvage:
    """What re-phasing a damaged frame produced.

    ``text`` is empty when no phase scored well enough to be worth showing —
    a short enough fragment says nothing no matter how it is aligned.  That is
    a separate outcome from "recovered text that happens to hold no code", and
    callers that care about the difference can read ``score``.
    """

    text: str = ""
    code: str = ""
    fill_bits: int = 0
    score: float = 0.0


def readability(text: str) -> float:
    """Score 0..1 for how much like a real SMS a candidate decode reads.

    Two halves.  The first is the share of characters that belong to plain
    written English at all; a stream read on the wrong phase is dense with
    the alphabet's Greek and accented rows and scores badly here.  The second
    is the share of characters sitting inside something word-shaped — a run of
    letters with a vowel in it, or a run of digits.  That second half is what
    separates a wrong phase that happens to land on printable characters
    (``Plw:9Pc4:d:1Pp::t27z``) from a right one (``Your GitHub``).
    """
    if not text:
        return 0.0
    plain = sum(c in _PLAIN for c in text) / len(text)
    words = sum(
        len(w)
        for w in _WORD.findall(text)
        if 2 <= len(w) <= 15 and _VOWEL.search(w)
    )
    digits = sum(len(d) for d in _DIGIT_RUN.findall(text))
    return 0.5 * plain + 0.5 * min(1.0, (words + digits) / len(text))


def extract_code(text: str) -> str:
    """Pull the verification code out of recovered text, or "" if there is none.

    A code-shaped run of digits is not enough on its own — a phone number and
    a reference number are made of the same digits — so the text has to say
    somewhere that it carries a code, and of several candidates the one
    nearest to where it says so wins.

    This errs towards returning nothing.  A caller that gets "" still has
    ``recovered_text`` to read, whereas a wrong number in a field called
    ``code`` is one a person would act on.  On a truncated frame an empty
    result usually means the code was in the octets the modem dropped, and it
    never means the sender did not send one.
    """
    candidates = list(_CODE.finditer(text))
    contexts = [m.start() for m in _CODE_CONTEXT.finditer(text)]
    if not candidates or not contexts:
        return ""
    return min(
        candidates, key=lambda m: min(abs(m.start() - c) for c in contexts)
    ).group()


def recover(body: bytes) -> Salvage:
    """Re-phase ``body`` as GSM 7-bit and return the most readable alignment.

    ``body`` is everything the frame carried after the originating address —
    on a damaged frame that is all user data, whatever the nominal field
    layout claims.  Trailing ``@`` is dropped: septet 0 is what the modem's
    own zero padding decodes to, and it is never message content at the end
    of a fragment.
    """
    best = Salvage()
    for fill_bits in _PHASES:
        septets = (len(body) * 8 - fill_bits) // 7
        if septets <= 0:
            continue
        text = gsm7.decode(gsm7.unpack(body, septets, fill_bits)).rstrip("@")
        score = readability(text)
        if score > best.score:
            best = Salvage(text=text, fill_bits=fill_bits, score=score)

    if best.score < _MIN_SCORE or len(best.text) < _MIN_CHARS:
        # Keep the score: the caller still knows the frame is damaged, it just
        # has nothing trustworthy to show for it.
        return Salvage(fill_bits=best.fill_bits, score=best.score)

    return Salvage(
        text=best.text,
        code=extract_code(best.text),
        fill_bits=best.fill_bits,
        score=best.score,
    )
