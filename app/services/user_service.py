from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.email_address import normalize_email
from app.core.security import hash_password
from app.models.user import Role, User
from app.schemas.user import UserCreate, UserUpdate


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Look up by canonical address, so case cannot create a second account.

    `lower()` rather than an exact match because accounts created before
    normalization existed may retain mixed-case addresses. New rows are always
    written normalized; this is the compatibility half of the same rule.
    """
    result = await db.execute(select(User).where(func.lower(User.email) == normalize_email(email)))
    return result.scalar_one_or_none()


async def create_user(db: AsyncSession, user_in: UserCreate, role: Role = Role.user) -> User:
    """Create a user.

    `password` is optional: public accounts are created through Google or an
    email code and have no password at all. It remains supported for staff
    accounts seeded by an administrator, which still authenticate with one.
    """
    user = User(
        email=normalize_email(user_in.email),
        hashed_password=hash_password(user_in.password) if user_in.password else None,
        full_name=user_in.full_name,
        role=role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def update_user(db: AsyncSession, user: User, user_in: "UserUpdate") -> User:
    """Apply a self-service profile edit. Only the display name is editable."""
    if user_in.full_name is not None:
        user.full_name = user_in.full_name or None

    await db.commit()
    await db.refresh(user)
    return user


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
