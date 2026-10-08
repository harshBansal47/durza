import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password, verify_password
from app.models.user import PasswordResetToken, Role, User
from app.schemas.user import UserCreate, UserUpdate
from app.services.email import ResetPasswordContent, send_reset_email


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def create_user(db: AsyncSession, user_in: UserCreate, role: Role = Role.user) -> User:
    user = User(
        email=user_in.email,
        hashed_password=hash_password(user_in.password),
        full_name=user_in.full_name,
        role=role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def update_user(db: AsyncSession, user: User, user_in: "UserUpdate") -> User:
    """Apply a profile edit. A password change requires the current one."""
    if user_in.password is not None:
        if user_in.current_password is None or not verify_password(
            user_in.current_password, user.hashed_password
        ):
            raise ValueError("Current password is incorrect")
        user.hashed_password = hash_password(user_in.password)

    if user_in.full_name is not None:
        user.full_name = user_in.full_name or None

    await db.commit()
    await db.refresh(user)
    return user


async def create_password_reset_token(db: AsyncSession, email: str) -> str | None:
    """Generate a password reset token for the user.

    Returns the plain token (to be emailed) or None if no such user.
    The token is single-use and expires in RESET_TOKEN_TTL_HOURS.
    """
    user = await get_user_by_email(db, email)
    if user is None:
        return None

    # Invalidate any existing tokens for this user
    await db.execute(
        PasswordResetToken.__table__.delete().where(PasswordResetToken.user_id == user.id)
    )

    plain_token = secrets.token_urlsafe(32)
    import hashlib

    token_hash = hashlib.sha256(plain_token.encode()).hexdigest()

    settings = get_settings()
    expires_at = datetime.now(UTC) + timedelta(hours=settings.reset_token_ttl_hours)

    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )
    await db.commit()

    return plain_token


async def consume_password_reset_token(db: AsyncSession, token: str) -> User | None:
    """Validate and consume a password reset token.

    Returns the user if the token is valid and not expired, otherwise None.
    The token is deleted on success (single-use).
    """
    import hashlib

    token_hash = hashlib.sha256(token.encode()).hexdigest()

    result = await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    )
    reset_token = result.scalar_one_or_none()

    if reset_token is None:
        return None

    if reset_token.expires_at < datetime.now(UTC):
        await db.delete(reset_token)
        await db.commit()
        return None

    user = await db.get(User, reset_token.user_id)
    if user is None or not user.is_active:
        return None

    await db.delete(reset_token)
    await db.commit()
    return user


async def send_password_reset_email(db: AsyncSession, email: str) -> bool:
    """Send a password reset email to the user.

    Returns True if the email was sent (or queued), False on failure.
    Always returns True for non-existent emails to avoid user enumeration.
    """
    plain_token = await create_password_reset_token(db, email)
    if plain_token is None:
        # Don't reveal whether the email exists
        return True

    settings = get_settings()
    reset_url = f"{settings.site_url.rstrip('/')}/account/reset-password?token={plain_token}"

    content = ResetPasswordContent(
        reset_url=reset_url,
        expires_hours=settings.reset_token_ttl_hours,
    )

    return await send_reset_email(email, content)


async def reset_password(db: AsyncSession, token: str, new_password: str) -> bool:
    """Reset the user's password using a valid token.

    Returns True on success, False if the token is invalid/expired.
    """
    if len(new_password) < 8:
        return False

    user = await consume_password_reset_token(db, token)
    if user is None:
        return False

    user.hashed_password = hash_password(new_password)
    await db.commit()
    return True


async def delete_user(db: AsyncSession, user: User) -> None:
    """Hard-delete a user and all their associated data.

    Verification reports are preserved (FK set to NULL) because they are
    public attestations — removing them would make success rates less honest.
    Everything else (saved stores/coupons, tracked products, alerts,
    notification preferences, push subscriptions) is deleted via cascade.
    """
    # The cascading deletes handle most related objects.
    # VerificationAttempt has ON DELETE SET NULL on coupon_id and user_id.
    await db.delete(user)
    await db.commit()
