"""Passwordless email sign-in codes.

Security properties this module is responsible for:

* Six digits from `secrets`, never `random`.
* Only an HMAC digest is stored; the plaintext exists long enough to build one
  message and is never logged.
* Short expiry, a small attempt budget, and one active challenge per address —
  requesting a new code invalidates every earlier one.
* Consumption is a conditional `UPDATE`, so two requests racing with the same
  code cannot both exchange it for a session.
* Requests for unknown addresses are indistinguishable from requests for known
  ones. Account creation happens only after the code proves inbox control, and
  always with the plain `user` role.
"""

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.email_address import digests_equal, normalize_email, otp_digest
from app.core.timezones import ensure_utc
from app.models.user import EmailOtpChallenge, Role, User
from app.services import user_service
from app.services.email import OtpEmailContent, send_otp_email


class InvalidOtpError(Exception):
    """The code is unknown, expired, already used, or out of attempts."""


class InactiveAccountError(Exception):
    """The address belongs to an account an administrator has disabled."""


def _generate_code() -> str:
    """Uniformly random six digits, including leading zeroes."""
    return f"{secrets.randbelow(1_000_000):06d}"


async def request_code(
    db: AsyncSession,
    *,
    email: str,
    request_ip_hash: str | None = None,
) -> bool:
    """Issue a code and email it.

    Returns whether a message was handed to the sender. Callers must return the
    same response either way: a cooldown, an hourly cap, a delivery failure and
    a successful send all look identical to the visitor. That is what keeps the
    endpoint from becoming an account-existence oracle.
    """
    settings = get_settings()
    normalized = normalize_email(email)
    now = datetime.now(UTC)

    # Per-address limits, on top of the per-IP limiter at the route. Both are
    # needed: an attacker with many addresses never trips the per-address cap,
    # and an attacker rotating IPs never trips the per-IP one.
    hour_ago = now - timedelta(hours=1)
    recent = await db.scalar(
        select(func.count())
        .select_from(EmailOtpChallenge)
        .where(
            EmailOtpChallenge.email == normalized,
            EmailOtpChallenge.created_at >= hour_ago,
        )
    )
    if (recent or 0) >= settings.otp_requests_per_email_per_hour:
        return False

    latest = await db.scalar(
        select(EmailOtpChallenge.created_at)
        .where(EmailOtpChallenge.email == normalized)
        .order_by(EmailOtpChallenge.created_at.desc())
        .limit(1)
    )
    if latest is not None:
        elapsed = now - ensure_utc(latest)
        if elapsed < timedelta(seconds=settings.otp_resend_cooldown_seconds):
            return False

    # One live code per address. This is both a user-experience rule (the most
    # recent email is always the one that works) and a security rule (an old
    # code cannot be replayed after a resend).
    await db.execute(
        update(EmailOtpChallenge)
        .where(
            EmailOtpChallenge.email == normalized,
            EmailOtpChallenge.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    # Old, already-spent rows have no further use. Keep a day for abuse
    # investigation, then stop accumulating a per-address mail history.
    await db.execute(
        delete(EmailOtpChallenge).where(
            EmailOtpChallenge.email == normalized,
            EmailOtpChallenge.created_at < now - timedelta(days=1),
        )
    )

    code = _generate_code()
    challenge = EmailOtpChallenge(
        email=normalized,
        code_hash=otp_digest(normalized, code),
        expires_at=now + timedelta(minutes=settings.otp_ttl_minutes),
        request_ip_hash=request_ip_hash,
    )
    db.add(challenge)
    await db.commit()
    await db.refresh(challenge)

    sent = await send_otp_email(
        normalized,
        OtpEmailContent(code=code, expires_minutes=settings.otp_ttl_minutes),
    )
    if not sent:
        # A code nobody received must not stay usable. Marking it consumed is
        # quieter than deleting it and keeps the attempt history for logs.
        await db.execute(
            update(EmailOtpChallenge)
            .where(EmailOtpChallenge.id == challenge.id)
            .values(consumed_at=datetime.now(UTC))
        )
        await db.commit()
    return sent


async def verify_code(db: AsyncSession, *, email: str, code: str) -> User:
    """Exchange a valid code for a user, creating the account if needed.

    Raises `InvalidOtpError` for every failure that must not distinguish one
    address from another, and `InactiveAccountError` only once a valid code has
    proved control of the inbox.
    """
    settings = get_settings()
    normalized = normalize_email(email)
    now = datetime.now(UTC)

    challenge = await db.scalar(
        select(EmailOtpChallenge)
        .where(
            EmailOtpChallenge.email == normalized,
            EmailOtpChallenge.consumed_at.is_(None),
        )
        .order_by(EmailOtpChallenge.created_at.desc())
        .limit(1)
    )
    if challenge is None:
        raise InvalidOtpError

    if ensure_utc(challenge.expires_at) <= now or challenge.attempts >= settings.otp_max_attempts:
        await _retire(db, challenge, now)
        raise InvalidOtpError

    if not digests_equal(otp_digest(normalized, code), challenge.code_hash):
        # Stop counting at the configured budget. The condition in SQL makes a
        # burst of parallel guesses unable to push the counter past it.
        await db.execute(
            update(EmailOtpChallenge)
            .where(
                EmailOtpChallenge.id == challenge.id,
                EmailOtpChallenge.consumed_at.is_(None),
                EmailOtpChallenge.attempts < settings.otp_max_attempts,
            )
            .values(attempts=EmailOtpChallenge.attempts + 1)
        )
        await db.commit()
        raise InvalidOtpError

    # Single-use consumption. Two requests holding the same correct code both
    # reach this statement; exactly one row matches `consumed_at IS NULL`, so
    # exactly one gets a session.
    consumed = await db.execute(
        update(EmailOtpChallenge)
        .where(
            EmailOtpChallenge.id == challenge.id,
            EmailOtpChallenge.consumed_at.is_(None),
            EmailOtpChallenge.attempts < settings.otp_max_attempts,
        )
        .values(consumed_at=now, attempts=EmailOtpChallenge.attempts + 1)
    )
    if getattr(consumed, "rowcount", 0) != 1:
        await db.rollback()
        raise InvalidOtpError

    user = await user_service.get_user_by_email(db, normalized)
    if user is None:
        user = User(
            email=normalized,
            hashed_password=None,
            role=Role.user,
            email_verified_at=now,
        )
        db.add(user)
    elif user.email_verified_at is None:
        # A code in this inbox is proof of control, whether the account was
        # created by Google or by an earlier password-era signup.
        user.email_verified_at = now

    try:
        await db.commit()
    except IntegrityError:
        # Another request created the same address between our lookup and
        # commit. Roll back (which also un-consumes the code) and re-read; the
        # unique index is the authority, not the earlier SELECT.
        await db.rollback()
        user = await user_service.get_user_by_email(db, normalized)
        if user is None:
            raise InvalidOtpError from None

    await db.refresh(user)

    if not user.is_active:
        raise InactiveAccountError

    return user


async def _retire(db: AsyncSession, challenge: EmailOtpChallenge, now: datetime) -> None:
    """Mark a challenge unusable, ignoring a concurrent retire."""
    await db.execute(
        update(EmailOtpChallenge)
        .where(
            EmailOtpChallenge.id == challenge.id,
            EmailOtpChallenge.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    await db.commit()
