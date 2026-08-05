"""Single-administrator authentication.

Deliberately minimal: one password, server-side sessions, no user table and
no password-free mode.  This system holds every SMS verification code that
reaches those SIMs, so password-free operation is deliberately not offered.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import Database, utcnow

PASSWORD_KEY = "auth.password"
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
# 128 * N * r is 32 MiB here, which is exactly OpenSSL's default ceiling —
# it refuses without headroom, so state the limit explicitly.
SCRYPT_MAXMEM = 64 * 1024 * 1024
SALT_BYTES = 16
SESSION_COOKIE = "hub_session"

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


class AuthError(Exception):
    pass


@dataclass
class PasswordPolicy:
    min_length: int = MIN_PASSWORD_LENGTH

    def check(self, password: str) -> None:
        if len(password) < self.min_length:
            raise AuthError(f"password must be at least {self.min_length} characters")
        if len(password) > MAX_PASSWORD_LENGTH:
            raise AuthError(f"password must be at most {MAX_PASSWORD_LENGTH} characters")
        classes = sum(
            (
                any(c.islower() for c in password),
                any(c.isupper() for c in password),
                any(c.isdigit() for c in password),
                any(not c.isalnum() for c in password),
            )
        )
        if classes < 2:
            raise AuthError(
                "password must mix at least two of: lowercase, uppercase, "
                "digits, symbols"
            )


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM, dklen=32,
    )


def _token_hash(token: str) -> str:
    # Sessions are stored hashed: a database leak must not hand out live logins.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Auth:
    def __init__(self, db: Database, *, session_ttl_hours: int = 24 * 14) -> None:
        self.db = db
        self.session_ttl = timedelta(hours=session_ttl_hours)
        self.policy = PasswordPolicy()

    # -- password ----------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        return self.db.get_setting(PASSWORD_KEY) is not None

    def set_password(self, password: str) -> None:
        self.policy.check(password)
        salt = secrets.token_bytes(SALT_BYTES)
        digest = _hash(password, salt)
        self.db.set_setting(
            PASSWORD_KEY, {"salt": salt.hex(), "hash": digest.hex(), "at": utcnow()}
        )
        # Any existing login is invalidated by a password change.
        self.revoke_all_sessions()

    def verify_password(self, password: str) -> bool:
        stored = self.db.get_setting(PASSWORD_KEY)
        if not isinstance(stored, dict):
            return False
        try:
            salt = bytes.fromhex(stored["salt"])
            expected = bytes.fromhex(stored["hash"])
        except (KeyError, ValueError):
            return False
        return hmac.compare_digest(_hash(password, salt), expected)

    def change_password(self, current: str, new: str) -> None:
        if not self.verify_password(current):
            raise AuthError("current password is incorrect")
        self.set_password(new)

    def clear(self) -> None:
        """Reset to first-run state (the CLI recovery path)."""
        self.db.execute("DELETE FROM settings WHERE key = ?", (PASSWORD_KEY,))
        self.revoke_all_sessions()

    # -- sessions ----------------------------------------------------------

    def create_session(self, label: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        self.db.execute(
            "INSERT INTO sessions (token_hash, created_at, expires_at, label) "
            "VALUES (?, ?, ?, ?)",
            (
                _token_hash(token),
                now.isoformat(timespec="seconds"),
                (now + self.session_ttl).isoformat(timespec="seconds"),
                label,
            ),
        )
        return token

    def validate_session(self, token: str | None) -> bool:
        if not token:
            return False
        row = self.db.one(
            "SELECT expires_at FROM sessions WHERE token_hash = ?",
            (_token_hash(token),),
        )
        if row is None:
            return False
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except ValueError:
            return False
        if expires <= datetime.now(timezone.utc):
            self.revoke_session(token)
            return False
        return True

    def revoke_session(self, token: str) -> None:
        self.db.execute(
            "DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),)
        )

    def revoke_all_sessions(self) -> None:
        self.db.execute("DELETE FROM sessions")

    def purge_expired_sessions(self) -> int:
        return self.db.execute(
            "DELETE FROM sessions WHERE expires_at < ?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
        ).rowcount


def verify_agent_token(presented: str | None, expected: str) -> bool:
    """Constant-time comparison of the agent's bearer token."""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)


def hash_agent_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_agent_token_hash(presented: str | None, expected_hash: str) -> bool:
    if not presented or not expected_hash:
        return False
    return hmac.compare_digest(hash_agent_token(presented), expected_hash)
