from datetime import UTC, datetime, timedelta
from typing import Any

from jose import jwt
from passlib.context import CryptContext

from app.core.config import get_settings

settings = get_settings()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def _create_token(
    subject: str,
    role: str,
    token_type: str,
    expires_delta: timedelta,
    token_version: int = 0,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "type": token_type,
        "ver": token_version,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def create_access_token(subject: str, role: str, token_version: int = 0) -> str:
    return _create_token(
        subject,
        role,
        "access",
        timedelta(minutes=settings.access_token_expire_minutes),
        token_version,
    )


def create_refresh_token(subject: str, role: str, token_version: int = 0) -> str:
    return _create_token(
        subject,
        role,
        "refresh",
        timedelta(days=settings.refresh_token_expire_days),
        token_version,
    )


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT. Raises jose.JWTError if invalid, expired, or malformed."""
    return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
