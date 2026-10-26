"""Email preferences changed from outside a session (the unsubscribe link)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.unsubscribe import read_unsubscribe_token
from app.models.tracking import NotificationPreference
from app.services.user_service import get_user_by_email


async def unsubscribe_by_token(db: AsyncSession, token: str) -> bool:
    """Switch off alert email for the address a signed token was issued for.

    Returns False only for a forged or damaged token. A valid token for an
    account that no longer exists still returns True: the person's goal —
    never hear from us at that address again — is already met, and an error
    page on an unsubscribe link is the wrong last impression.
    """
    email = read_unsubscribe_token(token)
    if email is None:
        return False

    user = await get_user_by_email(db, email)
    if user is None:
        return True

    # Preferences are created lazily by the alert engine. Updating only an
    # existing row would lose an unsubscribe from someone who has none yet, and
    # the first alert would arrive anyway.
    prefs = await db.scalar(
        select(NotificationPreference).where(NotificationPreference.user_id == user.id)
    )
    if prefs is None:
        db.add(NotificationPreference(user_id=user.id, email_enabled=False))
    else:
        prefs.email_enabled = False
    await db.commit()
    return True
