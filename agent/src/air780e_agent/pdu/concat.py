"""Reassembly of concatenated (long) SMS.

Segments of one long message arrive as separate PDUs sharing a reference
number.  They can arrive out of order, and the tail can go missing entirely
if the sender's network drops it — so every partial message carries a
deadline, after which we surface whatever we have rather than losing it.

The 30 second default follows chenxuuu/sms_forwarding, which has had a lot
more field exposure to Chinese carriers than we have.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .codec import DecodedSms

DEFAULT_TIMEOUT = 30.0


@dataclass
class _Partial:
    total: int
    address: str
    parts: dict[int, DecodedSms] = field(default_factory=dict)
    first_seen: float = field(default_factory=time.monotonic)

    @property
    def complete(self) -> bool:
        return len(self.parts) == self.total

    def merge(self) -> DecodedSms:
        ordered = [self.parts[i] for i in sorted(self.parts)]
        head = ordered[0]
        return DecodedSms(
            kind=head.kind,
            address=head.address,
            text="".join(p.text for p in ordered),
            smsc=head.smsc,
            timestamp=head.timestamp,
            dcs=head.dcs,
            alphabet=head.alphabet,
            concat=head.concat,
            raw=" ".join(p.raw for p in ordered),
        )


class Reassembler:
    """Collects multipart SMS until complete or timed out.

    ``push`` returns a message as soon as it is whole; single-part messages
    pass straight through.  Call ``flush_expired`` periodically to drain
    segments whose siblings never showed up.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout
        self._pending: dict[tuple[str, int], _Partial] = {}

    def push(self, sms: DecodedSms) -> DecodedSms | None:
        concat = sms.concat
        if concat is None or concat.total <= 1:
            return sms

        # Key on sender too: two senders can pick the same reference byte.
        key = (sms.address, concat.ref)
        partial = self._pending.get(key)
        if partial is None or partial.total != concat.total:
            partial = _Partial(total=concat.total, address=sms.address)
            self._pending[key] = partial

        partial.parts[concat.seq] = sms

        if partial.complete:
            del self._pending[key]
            return partial.merge()
        return None

    def flush_expired(self) -> list[DecodedSms]:
        """Give up on stale partials and return them merged with gaps closed."""
        now = time.monotonic()
        expired = [
            key
            for key, partial in self._pending.items()
            if now - partial.first_seen >= self._timeout
        ]
        out: list[DecodedSms] = []
        for key in expired:
            out.append(self._pending.pop(key).merge())
        return out

    @property
    def pending_count(self) -> int:
        return len(self._pending)
