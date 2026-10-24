"""Email normalization and one-way digests for passwordless sign-in.

The database stores a normalized address and an HMAC of each sign-in code —
never the code itself. HMAC (rather than a bare hash) means a leaked database
row cannot be brute-forced offline: six digits is a tiny keyspace, and the
secret is the only thing standing between a dump and every live code.
"""

import hashlib
import hmac

from app.core.config import get_settings


def normalize_email(email: str) -> str:
    """The canonical form used for lookup, storage, and code binding.

    Case and surrounding whitespace are not semantically meaningful in the
    addresses this product accepts, and treating them as meaningful creates
    duplicate accounts that both "own" one inbox.
    """
    return email.strip().lower()


def otp_digest(email: str, code: str) -> str:
    """HMAC-SHA256 of a code, bound to the address it was sent to.

    Binding the address prevents a code issued for one account from being
    replayed against another whose challenge row was somehow exposed.
    """
    settings = get_settings()
    payload = f"{normalize_email(email)}\0{code}".encode()
    return hmac.new(settings.otp_hash_secret.encode(), payload, hashlib.sha256).hexdigest()


def digests_equal(left: str, right: str) -> bool:
    """Constant-time comparison for two hex digests."""
    return hmac.compare_digest(left, right)
