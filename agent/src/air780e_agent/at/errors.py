"""Exceptions for the AT layer."""

from __future__ import annotations

# The subset of +CME / +CMS codes worth naming in a log line.  Anything else
# is reported numerically rather than guessed at.
CME_ERRORS = {
    3: "operation not allowed",
    4: "operation not supported",
    10: "SIM not inserted",
    11: "SIM PIN required",
    12: "SIM PUK required",
    13: "SIM failure",
    14: "SIM busy",
    15: "SIM wrong",
    16: "incorrect password",
    20: "memory full",
    21: "invalid index",
    22: "not found",
    23: "memory failure",
    30: "no network service",
    31: "network timeout",
    50: "incorrect parameters",
    100: "unknown",
}

CMS_ERRORS = {
    28: "unidentified subscriber",
    30: "unknown subscriber",
    38: "network out of order",
    41: "temporary failure",
    42: "congestion",
    47: "resources unavailable",
    50: "requested facility not subscribed",
    69: "requested facility not implemented",
    81: "invalid short message transfer reference value",
    95: "invalid message, unspecified",
    96: "invalid mandatory information",
    128: "telephony interworking not supported",
    129: "short message type 0 not supported",
    159: "unspecified TP-PID error",
    172: "SIM SMS storage full",
    177: "memory capacity exceeded",
    255: "unspecified error",
    300: "ME failure",
    302: "operation not allowed",
    304: "invalid PDU mode parameter",
    305: "invalid text mode parameter",
    310: "SIM not inserted",
    311: "SIM PIN required",
    313: "SIM failure",
    321: "invalid memory index",
    322: "SIM memory full",
    330: "SMSC address unknown",
    331: "no network service",
    332: "network timeout",
    500: "unknown error",
}


def _normalize(text: str) -> str:
    return " ".join(text.split()).rstrip(".").casefold()


# Reverse lookups, so a text-mode answer still yields a code.  ``AT+CMEE=1``
# is supposed to make the modem answer numerically, but Air780E firmware
# V1011 replies with text regardless; without this the code is lost and
# "no network service" (331) reads the same as "unknown error" (500).
_CME_CODES = {_normalize(name): code for code, name in CME_ERRORS.items()}
_CMS_CODES = {_normalize(name): code for code, name in CMS_ERRORS.items()}


def code_for_error_text(family: str, text: str) -> int | None:
    """The ``+CME``/``+CMS`` code whose canonical name is ``text``.

    Returns ``None`` for wording this table does not know, so an unrecognized
    string is reported verbatim rather than mapped to a plausible-looking code.
    """
    table = _CMS_CODES if family.upper() == "CMS" else _CME_CODES
    return table.get(_normalize(text))


class ATError(Exception):
    """Base class for AT command failures."""

    def __init__(self, message: str, *, command: str | None = None) -> None:
        self.command = command
        super().__init__(f"{command}: {message}" if command else message)


class ATTimeout(ATError):
    """The modem did not produce a final result code in time."""


class ATCommandError(ATError):
    """The modem replied with ``ERROR`` or another unstructured failure code."""

    def __init__(self, message: str, *, command: str | None = None) -> None:
        # The final line exactly as the modem sent it, kept apart from the
        # formatted message so callers can switch on it.  ATD ends on
        # NO CARRIER / BUSY / NO ANSWER, and for a keep-alive call those are
        # outcomes rather than failures; recovering them by unpicking
        # str(exc) would mean re-deriving what we already had in hand.
        self.final = message.strip().upper()
        super().__init__(message, command=command)


class CmeError(ATError):
    """``+CME ERROR: <code>`` — equipment-level failure."""

    def __init__(self, code: int, *, command: str | None = None) -> None:
        self.code = code
        name = CME_ERRORS.get(code, "unknown")
        super().__init__(f"+CME ERROR {code} ({name})", command=command)


class CmsError(ATError):
    """``+CMS ERROR: <code>`` — SMS-related failure."""

    def __init__(self, code: int, *, command: str | None = None) -> None:
        self.code = code
        name = CMS_ERRORS.get(code, "unknown")
        super().__init__(f"+CMS ERROR {code} ({name})", command=command)


class TransportClosed(ATError):
    """The serial port went away (module unplugged, USB reset)."""
