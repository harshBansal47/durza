"""Helpers for pulling privacy-conscious client metadata off a request."""

import hashlib

from starlette.requests import Request

from app.core.config import get_settings

settings = get_settings()


def client_ip(request: Request) -> str:
    # A reverse proxy (Nginx) sets this; fall back to the direct connection.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def hash_ip(ip: str) -> str:
    """One-way hash of an IP address, so we can rate-limit without storing it.

    Salted with `ip_hash_salt` when one is configured, and with the secret key
    otherwise. The fallback is what keeps the default behaviour identical: a
    deployment that has not chosen a salt yet keeps hashing exactly as it did,
    and the salt can be introduced later without invalidating the hashes that
    are still inside their retention window.
    """
    salt = settings.ip_hash_salt or settings.secret_key
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()
