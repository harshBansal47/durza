"""Service-level guarantees for passwordless email codes.

These are the properties that must hold even if the HTTP layer changes: only a
digest is stored, the code is six digits, resends supersede old codes, expiry
and attempts are enforced, a failed delivery cannot leave a usable code behind,
and successful verification creates a plain user.
"""

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.email_address import normalize_email, otp_digest
from app.models.user import EmailOtpChallenge, Role, User
from app.services import email_otp
from app.services.email import OtpEmailContent


class Outbox:
    def __init__(self, *, delivered: bool = True) -> None:
        self.delivered = delivered
        self.messages: list[tuple[str, OtpEmailContent]] = []

    async def __call__(self, to: str, content: OtpEmailContent) -> bool:
        self.messages.append((to, content))
        return self.delivered

    @property
    def last_code(self) -> str:
        return self.messages[-1][1].code


@pytest.fixture
def outbox(monkeypatch) -> Outbox:
    sent = Outbox()
    monkeypatch.setattr(email_otp, "send_otp_email", sent)
    return sent


@pytest.mark.asyncio
async def test_request_stores_only_an_hmac_digest(async_session_maker, outbox):
    async with async_session_maker() as db:
        sent = await email_otp.request_code(db, email="  Person@Example.COM ", request_ip_hash="ip")
        challenge = (await db.execute(select(EmailOtpChallenge))).scalar_one()

    assert sent is True
    assert outbox.messages[0][0] == "person@example.com"
    assert re.fullmatch(r"\d{6}", outbox.last_code)
    assert challenge.email == "person@example.com"
    assert challenge.code_hash != outbox.last_code
    assert challenge.code_hash == otp_digest("person@example.com", outbox.last_code)
    assert challenge.request_ip_hash == "ip"
    assert challenge.attempts == 0
    assert challenge.consumed_at is None


@pytest.mark.asyncio
async def test_resend_cooldown_suppresses_the_second_message(async_session_maker, outbox):
    async with async_session_maker() as db:
        first = await email_otp.request_code(db, email="cooldown@example.com")
        second = await email_otp.request_code(db, email="cooldown@example.com")
        challenges = (await db.execute(select(EmailOtpChallenge))).scalars().all()

    assert first is True
    assert second is False
    assert len(outbox.messages) == 1
    assert len(challenges) == 1


@pytest.mark.asyncio
async def test_hourly_per_address_cap_is_enforced(async_session_maker, outbox, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "otp_resend_cooldown_seconds", 0)
    monkeypatch.setattr(settings, "otp_requests_per_email_per_hour", 2)

    async with async_session_maker() as db:
        outcomes = [await email_otp.request_code(db, email="cap@example.com") for _ in range(3)]

    assert outcomes == [True, True, False]
    assert len(outbox.messages) == 2


@pytest.mark.asyncio
async def test_successful_verification_creates_a_plain_user(
    async_session_maker, outbox, monkeypatch
):
    monkeypatch.setattr(get_settings(), "otp_resend_cooldown_seconds", 0)
    async with async_session_maker() as db:
        await email_otp.request_code(db, email="new@example.com")
        code = outbox.last_code
        user = await email_otp.verify_code(db, email="NEW@example.com", code=code)

        assert user.email == "new@example.com"
        assert user.role is Role.user
        assert user.hashed_password is None
        assert user.email_verified_at is not None

    # The same address on a later sign-in resolves to the same row.
    async with async_session_maker() as db:
        await email_otp.request_code(db, email="new@example.com")
        again = await email_otp.verify_code(db, email="new@example.com", code=outbox.last_code)
        users = (await db.execute(select(User))).scalars().all()

    assert again.id == user.id
    assert len(users) == 1


@pytest.mark.asyncio
async def test_expired_code_is_rejected(async_session_maker, outbox):
    async with async_session_maker() as db:
        await email_otp.request_code(db, email="expired@example.com")
        code = outbox.last_code
        challenge = (await db.execute(select(EmailOtpChallenge))).scalar_one()
        challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

        with pytest.raises(email_otp.InvalidOtpError):
            await email_otp.verify_code(db, email="expired@example.com", code=code)


@pytest.mark.asyncio
async def test_attempt_budget_blocks_even_the_correct_code(async_session_maker, outbox):
    async with async_session_maker() as db:
        await email_otp.request_code(db, email="attempts@example.com")
        code = outbox.last_code
        wrong = "000000" if code != "000000" else "111111"

        for _ in range(get_settings().otp_max_attempts):
            with pytest.raises(email_otp.InvalidOtpError):
                await email_otp.verify_code(db, email="attempts@example.com", code=wrong)

        with pytest.raises(email_otp.InvalidOtpError):
            await email_otp.verify_code(db, email="attempts@example.com", code=code)


@pytest.mark.asyncio
async def test_failed_delivery_retires_the_code(async_session_maker, monkeypatch):
    failed = Outbox(delivered=False)
    monkeypatch.setattr(email_otp, "send_otp_email", failed)

    async with async_session_maker() as db:
        sent = await email_otp.request_code(db, email="undelivered@example.com")
        code = failed.last_code

        with pytest.raises(email_otp.InvalidOtpError):
            await email_otp.verify_code(db, email="undelivered@example.com", code=code)

        challenge = (await db.execute(select(EmailOtpChallenge))).scalar_one()

    assert sent is False
    assert challenge.consumed_at is not None


def test_digest_is_bound_to_the_address():
    """A digest from one inbox must not validate against another's challenge."""
    assert otp_digest("one@example.com", "123456") != otp_digest("two@example.com", "123456")


def test_normalization_lowercases_and_trims():
    assert normalize_email("  Someone@Example.COM ") == "someone@example.com"
