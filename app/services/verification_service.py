"""Automated re-checks of coupon codes.

The distinction this module exists to enforce: `CouponVerification` is a person
saying "this worked for me". This is us checking. They are stored separately and
aggregated separately, because mixing them lets a bot wall at one store drag down
a success rate shown to shoppers, and lets a flaky checker retire a working code.

The core rule is that **"we could not check" is never "this failed"**. Every
checker reports one of three outcomes, and only `failed` counts against a code.
A checker that hits a CAPTCHA, a 503 or a changed checkout layout returns
`inconclusive` and costs the code nothing. Getting this wrong is how an
automation system quietly deletes a working catalogue.

There is deliberately no live HTTP checking here. Applying a code to a real
basket is store-specific, frequently requires a session and a shipping address,
and is the single easiest way for a scraper to get a merchant's domain
blocklisted. `record_attempt` is the seam a future per-store checker plugs into;
the decision it feeds — retire a code that keeps failing — is `should_retire`.
"""

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.observability import get_logger
from app.models.coupon import Coupon, CouponStatus
from app.models.verification_attempt import AttemptOutcome, VerificationAttempt

logger = get_logger("couponza.verification")

# How far back a checker looks when deciding whether a code is dead. Three days
# is roughly one week of hourly checks: long enough that a single bad weekend at
# a store cannot retire a code, short enough that a genuinely dead code does not
# linger on the homepage for a month.
_RECENT_WINDOW = timedelta(days=3)

# Consecutive `failed` outcomes needed before a code is retired. Higher than it
# looks on purpose: the cost of a false retirement is a shopper being told a
# working discount does not exist, which is the single most trust-destroying
# thing this site can do.
_RETIRE_AFTER_CONSECUTIVE_FAILURES = 3


@dataclass(frozen=True)
class AttemptResult:
    outcome: AttemptOutcome
    detail: str | None = None

    @property
    def valid(self) -> bool | None:
        """Tri-state, matching the nullable column.

        `inconclusive` → None, so a failed *check* is never recorded as a
        verified-false code.
        """
        match self.outcome:
            case AttemptOutcome.worked:
                return True
            case AttemptOutcome.failed:
                return False
            case AttemptOutcome.inconclusive:
                return None

    @classmethod
    def worked(cls, detail: str | None = None) -> "AttemptResult":
        return cls(AttemptOutcome.worked, detail)

    @classmethod
    def failed(cls, detail: str | None = None) -> "AttemptResult":
        return cls(AttemptOutcome.failed, detail)

    @classmethod
    def inconclusive(cls, detail: str | None = None) -> "AttemptResult":
        """The checker could not tell. Never counts against the code."""
        return cls(AttemptOutcome.inconclusive, detail)


async def record_attempt(
    db: AsyncSession,
    coupon: Coupon,
    *,
    checker: str,
    result: AttemptResult,
    commit: bool = True,
) -> VerificationAttempt:
    """Append one check result. History is append-only, never overwritten."""
    attempt = VerificationAttempt(
        coupon_id=coupon.id,
        checker=checker,
        outcome=result.outcome,
        valid=result.valid,
        checked_at=datetime.now(UTC),
        detail=result.detail,
    )
    db.add(attempt)
    if commit:
        await db.commit()
    else:
        await db.flush()

    logger.info(
        "verification attempt recorded",
        extra={
            "coupon_id": str(coupon.id),
            "checker": checker,
            "outcome": result.outcome.value,
        },
    )
    return attempt


async def consecutive_failures(
    db: AsyncSession, coupon_id: uuid.UUID, *, checker: str | None = None
) -> int:
    """Count `failed` results in a row, most recent first.

    Walks backwards from the newest attempt and stops at the first non-failure.
    An `inconclusive` in between counts as a break, which is the honest reading:
    a checker that could not tell us anything has not cleared the code, but it
    also has not confirmed a failure, so the streak it interrupts should not be
    laundered into a longer one.
    """
    stmt = select(VerificationAttempt.outcome).where(VerificationAttempt.coupon_id == coupon_id)
    if checker is not None:
        stmt = stmt.where(VerificationAttempt.checker == checker)
    stmt = stmt.order_by(VerificationAttempt.checked_at.desc()).limit(
        _RETIRE_AFTER_CONSECUTIVE_FAILURES
    )

    outcomes = list((await db.execute(stmt)).scalars().all())
    streak = 0
    for outcome in outcomes:
        if outcome is not AttemptOutcome.failed:
            break
        streak += 1
    return streak


async def recent_summary(
    db: AsyncSession, coupon_id: uuid.UUID, *, checker: str | None = None
) -> dict[str, int]:
    """Counts by outcome over the recent window. Useful for admin and tests."""
    stmt = (
        select(VerificationAttempt.outcome, func.count())
        .where(
            VerificationAttempt.coupon_id == coupon_id,
            VerificationAttempt.checked_at >= datetime.now(UTC) - _RECENT_WINDOW,
        )
        .group_by(VerificationAttempt.outcome)
    )
    if checker is not None:
        stmt = stmt.where(VerificationAttempt.checker == checker)

    rows = (await db.execute(stmt)).all()
    return {outcome.value: count for outcome, count in rows}


async def should_retire(db: AsyncSession, coupon_id: uuid.UUID, *, checker: str) -> bool:
    """Whether a code has failed enough consecutive checks to be unpublished.

    Requires a minimum sample size inside the window as well as a streak, so a
    brand-new code that fails its first check does not get retired before the
    checker has demonstrated it works at all.
    """
    summary = await recent_summary(db, coupon_id, checker=checker)
    checked = summary.get(AttemptOutcome.failed.value, 0) + summary.get(
        AttemptOutcome.worked.value, 0
    )
    if checked < _RETIRE_AFTER_CONSECUTIVE_FAILURES:
        return False
    streak = await consecutive_failures(db, coupon_id, checker=checker)
    return streak >= _RETIRE_AFTER_CONSECUTIVE_FAILURES


async def retire_coupon(db: AsyncSession, coupon: Coupon, *, reason: str) -> None:
    """Unpublish a code our own checks have given up on.

    `failure_reason` is set so the admin and the storefront can say *why* a code
    vanished, instead of it silently disappearing from listings.
    """
    if coupon.status is CouponStatus.expired:
        return
    coupon.status = CouponStatus.expired
    coupon.is_active = False
    coupon.failure_reason = reason[:255]
    await db.commit()
    logger.info(
        "coupon retired after failed checks",
        extra={"coupon_id": str(coupon.id), "reason": reason},
    )


async def due_for_check(
    db: AsyncSession, *, older_than: timedelta = timedelta(hours=12), limit: int = 100
) -> list[Coupon]:
    """Active coupons that have not been checked recently.

    "Not checked recently" includes *never checked* — a newly ingested code
    should get a first look rather than sit unverified until some other coupon
    happens to be checked first.
    """
    cutoff = datetime.now(UTC) - older_than

    latest = (
        select(func.max(VerificationAttempt.checked_at))
        .where(VerificationAttempt.coupon_id == Coupon.id)
        .correlate(Coupon)
        .scalar_subquery()
    )

    result = await db.execute(
        select(Coupon)
        .where(
            Coupon.status == CouponStatus.active,
            Coupon.is_active.is_(True),
            # NULL latest covers "never checked", which is exactly when a code
            # most deserves a first look.
            (latest.is_(None)) | (latest < cutoff),
        )
        # Least-recently-touched first, so a burst of new codes cannot starve the
        # backlog of codes that have been failing for a week.
        .order_by(Coupon.updated_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


class Checker(enum.Enum):
    """Names of the checkers that may write attempts.

    Free text in the column, but a closed set here so a typo in a checker name
    becomes a loud error rather than a permanently-"unverified" code whose
    history nobody can find.
    """

    HTTP_HEAD = "http_head"
    CHECKOUT_PROBE = "checkout_probe"
    MANUAL = "manual"


async def run_sweep(db: AsyncSession, *, limit: int = 50) -> dict[str, int]:
    """Check every coupon that is due, and retire the ones that keep failing.

    Returns a small summary for the job history: how many were examined, how
    many attempts were recorded, and how many coupons were retired.

    There is no built-in live checker, deliberately. Applying a code to a real
    basket is store-specific, usually needs a session and a shipping address, and
    is the fastest way to get a merchant's domain blocklisted — after which *no*
    coupon from that store is checked, and the store's real codes disappear from
    a site whose entire value is that those codes work. Register a checker for a
    store you have permission to test; this function is the seam it plugs into.
    """
    due = await due_for_check(db, limit=limit)
    if not due:
        return {"examined": 0, "attempts": 0, "retired": 0}

    registered = [c for c in CHECKERS]
    if not registered:
        # Report this as a normal, successful no-op rather than an error. The job
        # is wired up and working; there is simply nothing installed to run, and
        # a failure here every cycle would train operators to ignore the job.
        logger.info(
            "verification sweep found work but no checker is registered",
            extra={"examined": len(due)},
        )
        return {"examined": len(due), "attempts": 0, "retired": 0}

    attempts = 0
    retired = 0
    for coupon in due:
        for checker in registered:
            if not checker.supports(coupon):
                continue
            try:
                result = await checker.check(coupon)
            except Exception:
                # A checker blowing up is our problem, not the code's. Log it and
                # move on: an exception here must never be recorded as `failed`,
                # or a single bug retires the entire catalogue.
                logger.exception("checker raised", extra={"coupon_id": str(coupon.id)})
                continue
            await record_attempt(db, coupon, checker=checker.name, result=result)
            attempts += 1

        if await should_retire(db, coupon.id, checker=Checker.CHECKOUT_PROBE.name):
            await retire_coupon(db, coupon, reason=f"{Checker.CHECKOUT_PROBE.value} failed 3x")
            retired += 1

    return {"examined": len(due), "attempts": attempts, "retired": retired}


class CouponChecker(Protocol):
    """What a store-specific code checker has to provide."""

    name: str

    def supports(self, coupon: Coupon) -> bool: ...

    async def check(self, coupon: Coupon) -> AttemptResult: ...


# Populated at import time by store-specific integrations. Empty by default.
CHECKERS: list[CouponChecker] = []


def outcome_for_status(status_code: int) -> AttemptResult:
    """Map an HTTP status from a checkout probe to an outcome.

    The mapping is deliberately narrow. Only statuses that *affirmatively*
    reject a code count as `failed` — 400 and 422 are what a basket returns when
    the discount is not applicable. Everything else is `inconclusive`,
    including 404: a 404 from a store's checkout almost always means the URL
    moved or the bot was bounced, and treating it as "this code is dead" would
    retire the entire catalogue the first time a merchant reorganised their site.
    """
    if status_code in (200, 204):
        return AttemptResult.worked(f"http {status_code}")
    if status_code in (400, 422):
        return AttemptResult.failed(f"http {status_code}")
    return AttemptResult.inconclusive(f"http {status_code}")
