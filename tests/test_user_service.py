"""User service: creation, lookup, profile updates, deletion.

Public accounts are passwordless now, so the interesting cases are that a user
can be created without a hash, that addresses are normalized on write and
looked up case-insensitively, and that the remaining profile field still clears
correctly. Password reset no longer exists and its tests went with it.
"""

import pytest
from sqlalchemy import select

from app.models.user import Role, User
from app.schemas.user import UserCreate, UserUpdate
from app.services import user_service

pytestmark = pytest.mark.asyncio


async def _make_user(db, email="user@example.com", password="supersecret123", **overrides):
    return await user_service.create_user(
        db, UserCreate(email=email, password=password), **overrides
    )


# ---- create / read -----------------------------------------------------------


async def test_create_staff_user_hashes_the_password(async_session_maker):
    async with async_session_maker() as db:
        user = await user_service.create_user(
            db, UserCreate(email="ada@example.com", password="supersecret123", full_name="Ada")
        )

    assert user.email == "ada@example.com"
    assert user.full_name == "Ada"
    assert user.role is Role.user
    assert user.hashed_password is not None
    assert user.hashed_password != "supersecret123"
    assert user.is_active is True
    assert user.email_verified_at is None


async def test_create_passwordless_user(async_session_maker):
    async with async_session_maker() as db:
        user = await user_service.create_user(db, UserCreate(email="otp@example.com"))

    assert user.hashed_password is None
    assert user.token_version == 0


async def test_create_user_normalizes_the_address(async_session_maker):
    async with async_session_maker() as db:
        user = await _make_user(db, email="  Mixed.Case@Example.COM ")

    assert user.email == "mixed.case@example.com"


async def test_get_user_by_email_is_case_insensitive(async_session_maker):
    async with async_session_maker() as db:
        await _make_user(db, email="found@example.com")
        found = await user_service.get_user_by_email(db, "FOUND@Example.com")
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


async def test_update_user_does_not_change_a_password_hash(async_session_maker):
    async with async_session_maker() as db:
        user = await _make_user(db)
        before = user.hashed_password
        await user_service.update_user(db, user, UserUpdate(full_name="Renamed"))

    assert user.hashed_password == before


# ---- delete_user -------------------------------------------------------------


async def test_delete_user_removes_the_row(async_session_maker):
    async with async_session_maker() as db:
        user = await _make_user(db, email="gone@example.com")
        await user_service.delete_user(db, user)

        remaining = (await db.execute(select(User))).scalars().all()

    assert remaining == []
