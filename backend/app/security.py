"""Password hashing and signed session tokens. Standard library only.

Passwords: scrypt (n=2**14, r=8, p=1) with a random 16-byte salt, stored as
"scrypt$<n>$<r>$<p>$<salt hex>$<hash hex>".

Sessions: a stateless token "<user id>.<issued at>.<signature>", signed with HMAC-SHA256 under
the session secret and held in an HttpOnly cookie. It expires after `max_age` seconds.
"""

import hashlib
import hmac
import secrets
import time
import uuid

_N, _R, _P = 2**14, 8, 1
_DKLEN = 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


# Checked against when the username does not exist, so a wrong username takes as long as a
# wrong password.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def verify_password(password: str, stored: str | None) -> bool:
    """True when `password` matches `stored`. Unknown or malformed hashes never match."""
    parts = (stored or _DUMMY_HASH).split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        parts = _DUMMY_HASH.split("$")
        stored = None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, expected = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=len(expected))
    return hmac.compare_digest(digest, expected) and stored is not None


def _sign(secret: bytes, message: str) -> str:
    return hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()


def make_session_token(user_id: uuid.UUID, secret: bytes, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    message = f"{user_id}.{issued}"
    return f"{message}.{_sign(secret, message)}"


def read_session_token(
    token: str, secret: bytes, max_age: int, now: float | None = None
) -> uuid.UUID | None:
    """The user id in a valid, unexpired token; None otherwise."""
    try:
        user_id, issued, signature = token.split(".")
        uid, issued_at = uuid.UUID(user_id), int(issued)
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _sign(secret, f"{user_id}.{issued}")):
        return None
    age = (now if now is not None else time.time()) - issued_at
    if age < 0 or age > max_age:
        return None
    return uid
