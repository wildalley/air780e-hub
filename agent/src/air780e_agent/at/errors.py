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


class ATError(Exception):
    """Base class for AT command failures."""

    def __init__(self, message: str, *, command: str | None = None) -> None:
        self.command = command
        super().__init__(f"{command}: {message}" if command else message)


class ATTimeout(ATError):
    """The modem did not produce a final result code in time."""


class ATCommandError(ATError):
    """The modem replied with a bare ``ERROR``."""


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
