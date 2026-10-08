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
    """One-way hash, salted with SECRET_KEY, so we rate-limit without storing raw IPs."""
    return hashlib.sha256(f"{settings.secret_key}:{ip}".encode()).hexdigest()
