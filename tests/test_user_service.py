"""User service: profile updates, password resets, deletion.

The reset-token tests are the security-critical ones: single-use, expiring,
and not enumerable. `send_password_reset_email` must return the same answer
for "email exists" and "email does not exist".
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.user import PasswordResetToken, Role, User
from app.schemas.user import UserCreate, UserUpdate
from app.services import user_service

pytestmark = pytest.mark.asyncio


async def _make_user(db, email="user@example.com", password="supersecret123", **overrides):
    return await user_service.create_user(
        db, UserCreate(email=email, password=password), **overrides
    )


# ---- create / read -----------------------------------------------------------


async def test_create_user_hashes_the_password(async_session_maker):
    async with async_session_maker() as db:
        user = await user_service.create_user(
            db, UserCreate(email="ada@example.com", password="supersecret123", full_name="Ada")
        )

    assert user.email == "ada@example.com"
    assert user.full_name == "Ada"
    assert user.role is Role.user
    assert user.hashed_password != "supersecret123"
    assert user.is_active is True


async def test_get_user_by_email_found_and_missing(async_session_maker):
    async with async_session_maker() as db:
        await _make_user(db, email="found@example.com")
        found = await user_service.get_user_by_email(db, "found@example.com")
        missing = await user_service.get_user_by_email(db, "nobody@example.com")

    assert found is not None and found.email == "found@example.com"
    assert missing is None


# ---- update_user -------------------------------------------------------------


async def test_update_user_full_name(async_session_maker):
    async with async_session_maker() as db:
        user = await _make_user(db)
        updated = await user_service.update_user(db, user, UserUpdate(full_name="New Name"))

    assert updated.full_name == "New Name"


async def test_update_user_clears_full_name_with_empty_string(async_session_maker):
    async with async_session_maker() as db:
        user = await _make_user(db)
        updated = await user_service.update_user(db, user, UserUpdate(full_name=""))

    assert updated.full_name is None


async def test_update_user_password_requires_correct_current_password(async_session_maker):
    async with async_session_maker() as db:
        user = await _make_user(db)

        with pytest.raises(ValueError, match="Current password is incorrect"):
            await user_service.update_user(
                db, user, UserUpdate(password="newpassword123", current_password="wrong")
            )

        with pytest.raises(ValueError, match="Current password is incorrect"):
            await user_service.update_user(db, user, UserUpdate(password="newpassword123"))


async def test_update_user_password_with_correct_current_password(async_session_maker):
    from app.core.security import verify_password

    async with async_session_maker() as db:
        user = await _make_user(db)
        updated = await user_service.update_user(
            db,
            user,
            UserUpdate(password="newpassword123", current_password="supersecret123"),
        )

    assert verify_password("newpassword123", updated.hashed_password)


# ---- password reset tokens ---------------------------------------------------


async def test_create_reset_token_returns_plain_token_and_stores_hash(async_session_maker):
    async with async_session_maker() as db:
        await _make_user(db, email="reset@example.com")
        token = await user_service.create_password_reset_token(db, "reset@example.com")

        assert token is not None
        rows = (await db.execute(select(PasswordResetToken))).scalars().all()

    assert len(rows) == 1
    assert rows[0].token_hash != token  # stored hashed, not in the clear


async def test_create_reset_token_for_unknown_email_returns_none(async_session_maker):
    async with async_session_maker() as db:
        token = await user_service.create_password_reset_token(db, "nobody@example.com")

    assert token is None


async def test_create_reset_token_invalidates_previous_tokens(async_session_maker):
    async with async_session_maker() as db:
        await _make_user(db, email="twice@example.com")
        first = await user_service.create_password_reset_token(db, "twice@example.com")
        await user_service.create_password_reset_token(db, "twice@example.com")

        rows = (await db.execute(select(PasswordResetToken))).scalars().all()

    assert len(rows) == 1  # the first was superseded
    assert await user_service.consume_password_reset_token(db, first) is None


async def test_consume_token_returns_user_once_then_none(async_session_maker):
    async with async_session_maker() as db:
        user = await _make_user(db, email="consume@example.com")
        token = await user_service.create_password_reset_token(db, "consume@example.com")

        first = await user_service.consume_password_reset_token(db, token)
        second = await user_service.consume_password_reset_token(db, token)

    assert first is not None and first.id == user.id
    assert second is None  # single-use


async def test_consume_token_rejects_expired_and_deletes_it(async_session_maker):
    async with async_session_maker() as db:
        await _make_user(db, email="expired@example.com")
        token = await user_service.create_password_reset_token(db, "expired@example.com")

        row = (await db.execute(select(PasswordResetToken))).scalar_one()
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()

        result = await user_service.consume_password_reset_token(db, token)
        remaining = (await db.execute(select(PasswordResetToken))).scalars().all()

    assert result is None
    assert remaining == []  # expired token is cleaned up


async def test_consume_token_rejects_inactive_user(async_session_maker):
    async with async_session_maker() as db:
        user = await _make_user(db, email="inactive@example.com")
        token = await user_service.create_password_reset_token(db, "inactive@example.com")

        user.is_active = False
        await db.commit()

        result = await user_service.consume_password_reset_token(db, token)

    assert result is None


async def test_consume_unknown_token_returns_none(async_session_maker):
    async with async_session_maker() as db:
        result = await user_service.consume_password_reset_token(db, "not-a-real-token")

    assert result is None


# ---- send_password_reset_email -----------------------------------------------


async def test_send_reset_email_returns_true_for_unknown_email(async_session_maker):
    """No user enumeration: the caller cannot distinguish missing from sent."""
    async with async_session_maker() as db:
        result = await user_service.send_password_reset_email(db, "nobody@example.com")

    assert result is True


async def test_send_reset_email_queues_for_known_user(async_session_maker, monkeypatch):
    sent: dict = {}

    async def fake_send(to: str, content) -> bool:
        sent["to"] = to
        sent["content"] = content
        return True

    monkeypatch.setattr(user_service, "send_reset_email", fake_send)

    async with async_session_maker() as db:
        await _make_user(db, email="mailme@example.com")
        result = await user_service.send_password_reset_email(db, "mailme@example.com")

    assert result is True
    assert sent["to"] == "mailme@example.com"
    assert "token=" in sent["content"].reset_url


# ---- reset_password ----------------------------------------------------------


async def test_reset_password_rejects_short_password(async_session_maker):
    async with async_session_maker() as db:
        result = await user_service.reset_password(db, "any-token", "short")

    assert result is False


async def test_reset_password_happy_path(async_session_maker):
    from app.core.security import verify_password

    async with async_session_maker() as db:
        user = await _make_user(db, email="reset-me@example.com")
        token = await user_service.create_password_reset_token(db, "reset-me@example.com")

        result = await user_service.reset_password(db, token, "brandnewpass123")
        await db.refresh(user)

    assert result is True
    assert verify_password("brandnewpass123", user.hashed_password)


async def test_reset_password_rejects_invalid_token(async_session_maker):
    async with async_session_maker() as db:
        result = await user_service.reset_password(db, "not-a-token", "brandnewpass123")

    assert result is False


# ---- delete_user -------------------------------------------------------------


async def test_delete_user_removes_the_row(async_session_maker):
    async with async_session_maker() as db:
        user = await _make_user(db, email="gone@example.com")
        await user_service.delete_user(db, user)

        remaining = (await db.execute(select(User))).scalars().all()

    assert remaining == []
