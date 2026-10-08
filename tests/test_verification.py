"""Automated coupon verification.

The property that matters most here is the negative one: **an inconclusive check
must never count as a failure.** Getting that wrong means one bad weekend at a
merchant retires their entire catalogue, and the site's whole value proposition
— "these codes actually work" — disappears at once.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.category import Category
from app.models.coupon import Coupon, CouponStatus, DiscountType
from app.models.store import Store
from app.models.verification_attempt import AttemptOutcome, VerificationAttempt
from app.services import verification_service as vs
from app.services.verification_service import AttemptResult

CHECKER = "checkout_probe"


async def _coupon(db, *, expires_in_days: int | None = 30) -> Coupon:
    store = Store(name="Store", slug=f"store-{uuid.uuid4().hex[:6]}")
    category = Category(name="Cat", slug=f"cat-{uuid.uuid4().hex[:6]}")
    db.add_all([store, category])
    await db.flush()
    coupon = Coupon(
        title="20% off everything",
        slug=f"deal-{uuid.uuid4().hex[:8]}",
        code="SAVE20",
        discount_type=DiscountType.percentage,
        discount_value=20.0,
        store_id=store.id,
        category_id=category.id,
        destination_url="https://example.com/deal",
        expires_at=None
        if expires_in_days is None
        else datetime.now(UTC) + timedelta(days=expires_in_days),
    )
    db.add(coupon)
    await db.commit()
    await db.refresh(coupon)
    return coupon


async def _record(db, coupon, result: AttemptResult, *, when: datetime | None = None) -> None:
    attempt = await vs.record_attempt(db, coupon, checker=CHECKER, result=result)
    if when is not None:
        # Rewriting the timestamp after the fact is the only way to build the
        # history these tests need; in production the value is set on insert.
        attempt.checked_at = when
        await db.commit()


# ---- Tri-state outcome ----


def test_valid_is_tri_state_not_a_boolean() -> None:
    # This is the whole point of the nullable column: "we could not check" must
    # not be expressible as a claim that the code is invalid.
    assert AttemptResult.worked().valid is True
    assert AttemptResult.failed().valid is False
    assert AttemptResult.inconclusive().valid is None


@pytest.mark.parametrize(
    ("status", "outcome", "valid"),
    [
        (200, AttemptOutcome.worked, True),
        (204, AttemptOutcome.worked, True),
        # A basket that affirmatively rejects the code.
        (400, AttemptOutcome.failed, False),
        (422, AttemptOutcome.failed, False),
        # Everything below is about *us* being blocked or the store misbehaving.
        # 404 in particular usually means the URL moved, not that the code died —
        # treating it as a failure is how one merchant's redesign retires
        # every code they have.
        (404, AttemptOutcome.inconclusive, None),
        (410, AttemptOutcome.inconclusive, None),
        (401, AttemptOutcome.inconclusive, None),
        (403, AttemptOutcome.inconclusive, None),
        (429, AttemptOutcome.inconclusive, None),
        (500, AttemptOutcome.inconclusive, None),
        (503, AttemptOutcome.inconclusive, None),
    ],
)
def test_status_mapping(status: int, outcome: AttemptOutcome, valid: bool | None) -> None:
    result = vs.outcome_for_status(status)
    assert result.outcome is outcome
    assert result.valid is valid


# ---- Recording ----


@pytest.mark.asyncio
async def test_recording_an_attempt_writes_one_row(async_session_maker) -> None:
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        await _record(db, coupon, AttemptResult.worked("http 200"))

        rows = list((await db.execute(select(VerificationAttempt))).scalars().all())
        assert len(rows) == 1
        assert rows[0].outcome is AttemptOutcome.worked
        assert rows[0].valid is True
        assert rows[0].detail == "http 200"


@pytest.mark.asyncio
async def test_history_is_append_only(async_session_maker) -> None:
    # Overwriting the newest result would destroy the pattern analysis that
    # justifies having a history table at all.
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        await _record(db, coupon, AttemptResult.failed("http 400"))
        await _record(db, coupon, AttemptResult.worked("http 200"))

        rows = list((await db.execute(select(VerificationAttempt))).scalars().all())
        assert len(rows) == 2
        assert {r.outcome for r in rows} == {AttemptOutcome.worked, AttemptOutcome.failed}


# ---- Failure streaks ----


@pytest.mark.asyncio
async def test_consecutive_failures_counts_a_streak(async_session_maker) -> None:
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        now = datetime.now(UTC)
        for i in range(3):
            await _record(db, coupon, AttemptResult.failed(), when=now - timedelta(minutes=10 - i))

        assert await vs.consecutive_failures(db, coupon.id, checker=CHECKER) == 3


@pytest.mark.asyncio
async def test_a_success_breaks_the_streak(async_session_maker) -> None:
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        now = datetime.now(UTC)
        await _record(db, coupon, AttemptResult.failed(), when=now - timedelta(minutes=30))
        await _record(db, coupon, AttemptResult.failed(), when=now - timedelta(minutes=20))
        await _record(db, coupon, AttemptResult.worked(), when=now - timedelta(minutes=10))
        await _record(db, coupon, AttemptResult.failed(), when=now)

        assert await vs.consecutive_failures(db, coupon.id, checker=CHECKER) == 1


@pytest.mark.asyncio
async def test_an_inconclusive_check_breaks_the_streak(async_session_maker) -> None:
    # Deliberate. A checker that could not tell us anything has neither cleared
    # nor confirmed the code, so it must not launder an old streak into a longer
    # new one — otherwise a checker that is blocked by a bot wall steadily
    # accumulates "failures" and retires the catalogue.
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        now = datetime.now(UTC)
        for i in range(2):
            await _record(db, coupon, AttemptResult.failed(), when=now - timedelta(minutes=30 - i))
        await _record(db, coupon, AttemptResult.inconclusive("bot wall"), when=now)
        await _record(db, coupon, AttemptResult.failed(), when=now + timedelta(minutes=1))

        assert await vs.consecutive_failures(db, coupon.id, checker=CHECKER) == 1


@pytest.mark.asyncio
async def test_streaks_are_scoped_per_checker(async_session_maker) -> None:
    # Two checkers disagreeing must not add up to a streak against the code.
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        now = datetime.now(UTC)
        await _record(db, coupon, AttemptResult.failed(), when=now)
        attempt = await vs.record_attempt(
            db, coupon, checker="http_head", result=AttemptResult.worked()
        )
        attempt.checked_at = now
        await db.commit()

        assert await vs.consecutive_failures(db, coupon.id, checker="checkout_probe") == 1
        assert await vs.consecutive_failures(db, coupon.id, checker="http_head") == 0


# ---- Retirement ----


@pytest.mark.asyncio
async def test_should_not_retire_on_a_short_streak(async_session_maker) -> None:
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        await _record(db, coupon, AttemptResult.failed())
        await _record(db, coupon, AttemptResult.failed())

        assert await vs.should_retire(db, coupon.id, checker=CHECKER) is False
        assert coupon.status is CouponStatus.active


@pytest.mark.asyncio
async def test_should_retire_after_three_consecutive_failures(async_session_maker) -> None:
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        for _ in range(3):
            await _record(db, coupon, AttemptResult.failed())

        assert await vs.should_retire(db, coupon.id, checker=CHECKER) is True


@pytest.mark.asyncio
async def test_streak_alone_is_not_enough_without_a_minimum_sample(
    async_session_maker,
) -> None:
    # A brand-new code must not be retired before the checker has demonstrated
    # that it works at all. `due_for_check` only returns one at a time, so a
    # streak can never reach three from a single sweep.
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        for _ in range(5):
            await _record(db, coupon, AttemptResult.inconclusive())

        assert await vs.should_retire(db, coupon.id, checker=CHECKER) is False


@pytest.mark.asyncio
async def test_retire_unpublishes_and_explains_why(async_session_maker) -> None:
    # The reason is stored so a code's disappearance is legible to an admin
    # instead of looking like a silent data loss.
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        await vs.retire_coupon(db, coupon, reason="checkout_probe failed 3x")

        await db.refresh(coupon)
        assert coupon.status is CouponStatus.expired
        assert coupon.is_active is False
        assert "failed 3x" in (coupon.failure_reason or "")


@pytest.mark.asyncio
async def test_retire_is_idempotent(async_session_maker) -> None:
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        await vs.retire_coupon(db, coupon, reason="first")
        await vs.retire_coupon(db, coupon, reason="second")

        await db.refresh(coupon)
        assert coupon.failure_reason == "first"


# ---- Due selection ----


@pytest.mark.asyncio
async def test_never_checked_coupons_are_due(async_session_maker) -> None:
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        due = await vs.due_for_check(db)
        assert [c.id for c in due] == [coupon.id]


@pytest.mark.asyncio
async def test_recently_checked_coupons_are_not_due(async_session_maker) -> None:
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        await _record(db, coupon, AttemptResult.worked())

        assert await vs.due_for_check(db) == []


@pytest.mark.asyncio
async def test_stale_checks_become_due_again(async_session_maker) -> None:
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        await _record(
            db, coupon, AttemptResult.worked(), when=datetime.now(UTC) - timedelta(days=2)
        )

        due = await vs.due_for_check(db)
        assert [c.id for c in due] == [coupon.id]


@pytest.mark.asyncio
async def test_expired_coupons_are_never_due(async_session_maker) -> None:
    async with async_session_maker() as db:
        coupon = await _coupon(db, expires_in_days=-1)
        await vs.retire_coupon(db, coupon, reason="expired")

        assert await vs.due_for_check(db) == []


@pytest.mark.asyncio
async def test_due_list_honours_the_limit(async_session_maker) -> None:
    async with async_session_maker() as db:
        for _ in range(4):
            await _coupon(db)

        assert len(await vs.due_for_check(db, limit=2)) == 2


# ---- Summary ----


@pytest.mark.asyncio
async def test_summary_counts_by_outcome(async_session_maker) -> None:
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        await _record(db, coupon, AttemptResult.worked())
        await _record(db, coupon, AttemptResult.failed())
        await _record(db, coupon, AttemptResult.inconclusive())

        summary = await vs.recent_summary(db, coupon.id, checker=CHECKER)
        assert summary == {"worked": 1, "failed": 1, "inconclusive": 1}


@pytest.mark.asyncio
async def test_summary_ignores_old_attempts(async_session_maker) -> None:
    # Keeps the "recent" in `recent_summary` honest; a code checked once a year
    # ago tells us nothing about today.
    async with async_session_maker() as db:
        coupon = await _coupon(db)
        await _record(
            db, coupon, AttemptResult.failed(), when=datetime.now(UTC) - timedelta(days=30)
        )

        assert await vs.recent_summary(db, coupon.id, checker=CHECKER) == {}


# ---- Sweep ----


@pytest.mark.asyncio
async def test_sweep_with_no_checkers_is_a_successful_no_op(async_session_maker) -> None:
    # The job is wired up and working; there is simply nothing installed to run.
    # Reporting this as an error every cycle would train operators to ignore it.
    async with async_session_maker() as db:
        await _coupon(db)
        summary = await vs.run_sweep(db)
        assert summary == {"examined": 1, "attempts": 0, "retired": 0}


@pytest.mark.asyncio
async def test_sweep_with_nothing_due_records_nothing(async_session_maker) -> None:
    async with async_session_maker() as db:
        assert await vs.run_sweep(db) == {"examined": 0, "attempts": 0, "retired": 0}


@pytest.mark.asyncio
async def test_a_checker_that_raises_is_not_recorded_as_a_failure(
    async_session_maker, monkeypatch
) -> None:
    # A bug in our checker must never cost a merchant their codes.
    class _Exploding:
        name = CHECKER

        def supports(self, coupon):
            return True

        async def check(self, coupon):
            raise RuntimeError("checker bug")

    monkeypatch.setattr(vs, "CHECKERS", [_Exploding()])

    async with async_session_maker() as db:
        coupon = await _coupon(db)
        summary = await vs.run_sweep(db)

        assert summary["attempts"] == 0
        assert summary["retired"] == 0
        assert await vs.should_retire(db, coupon.id, checker=CHECKER) is False


@pytest.mark.asyncio
async def test_sweep_records_a_working_checker_result(async_session_maker, monkeypatch) -> None:
    class _Working:
        name = CHECKER

        def __init__(self) -> None:
            self.calls = 0

        def supports(self, coupon):
            return True

        async def check(self, coupon):
            self.calls += 1
            return AttemptResult.worked("http 200")

    checker = _Working()
    monkeypatch.setattr(vs, "CHECKERS", [checker])

    async with async_session_maker() as db:
        await _coupon(db)
        summary = await vs.run_sweep(db)

        assert summary == {"examined": 1, "attempts": 1, "retired": 0}
        assert checker.calls == 1
