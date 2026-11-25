"""
Authentication primitives.

Deliberately stdlib-only (hashlib's PBKDF2-HMAC-SHA256, 100k iterations —
the same primitive Django's default password hasher used for years) rather
than pulling in bcrypt/argon2, so the project has one fewer thing that can
fail to build on an unusual platform. For a production deployment, argon2id
is the current best-practice upgrade — a drop-in swap of `hash_password`/
`verify_password` since nothing outside this file knows the hash format.

Sessions are opaque random tokens stored server-side (not JWTs) — simpler to
revoke (delete the row) and nothing meaningful to decode if leaked.
"""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta

from .timeutil import utcnow

PBKDF2_ITERATIONS = 100_000
SESSION_TTL = timedelta(days=14)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    return hmac.compare_digest(actual, expected)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_expiry() -> datetime:
    return utcnow() + SESSION_TTL
