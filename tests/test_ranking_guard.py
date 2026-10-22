"""The ranking version guard rails.

`RANKING_VERSION` picks the stored score the "recommended" listing sorts by.
The v2 column (`quality_score_v2`) is a shadow column that nothing writes, so
a v2 sort would order every coupon at the same score — and because a NULL
sorts **first** in a descending order, the listing would become "newest
first" without saying so.

Two things are pinned here:

* the **startup refusal** — v2 with an active coupon that has no v2 score is
  refused loudly, because a silently wrong ranking is worse than an outage
  that explains itself;
* the **query-time fallback** — a coupon with no v2 score sorts by its v1
  score, so it cannot land at the top of the listing just because its score
  is missing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.models.category import Category
from app.models.coupon import Coupon, DiscountType
from app.models.store import Store
from app.services import ranking
from app.services.coupon_service import CouponSort, list_coupons


async def _coupon(
    db,
    *,
    title: str,
    quality_score: int,
    quality_score_v2: int | None = None,
    is_active: bool = True,
    created_at: datetime | None = None,
    store: Store | None = None,
    category: Category | None = None,
) -> Coupon:
    if store is None:
        store = Store(name="Store", slug=f"store-{uuid.uuid4().hex[:6]}")
    if category is None:
        category = Category(name="Cat", slug=f"cat-{uuid.uuid4().hex[:6]}")
    db.add_all([store, category])
    await db.flush()
    coupon = Coupon(
        title=title,
        slug=f"deal-{uuid.uuid4().hex[:8]}",
        code="SAVE20",
        discount_type=DiscountType.percentage,
        discount_value=20.0,
        store_id=store.id,
        category_id=category.id,
        destination_url="https://example.com/deal",
        quality_score=quality_score,
        quality_score_v2=quality_score_v2,
        is_active=is_active,
        created_at=created_at or datetime.now(UTC),
    )
    db.add(coupon)
    await db.flush()
    return coupon


# --------------------------------------------------------------- the startup guard


async def test_v1_is_never_refused(async_session_maker) -> None:
    """v1 reads the column the pipeline fills, so there is nothing to check."""
    async with async_session_maker() as db:
        await _coupon(db, title="No v2 score", quality_score=40)
        missing = await ranking.assert_ranking_version_ready(db, ranking_version="v1")
    assert missing == 0


async def test_v2_is_refused_when_an_active_coupon_has_no_v2_score(async_session_maker) -> None:
    async with async_session_maker() as db:
        await _coupon(db, title="Scored once", quality_score=40, quality_score_v2=91)
        await _coupon(db, title="Never scored", quality_score=40)

        with pytest.raises(ranking.RankingVersionNotReadyError) as excinfo:
            await ranking.assert_ranking_version_ready(db, ranking_version="v2")

    message = str(excinfo.value)
    assert "quality_score_v2" in message
    assert "Nothing is backfilled automatically" in message


async def test_v2_is_allowed_when_every_active_coupon_has_a_v2_score(async_session_maker) -> None:
    """An inactive coupon cannot distort a listing, so it does not block."""
    async with async_session_maker() as db:
        await _coupon(db, title="Scored", quality_score=40, quality_score_v2=91)
        await _coupon(
            db,
            title="Retired",
            quality_score=40,
            is_active=False,
        )
        missing = await ranking.assert_ranking_version_ready(db, ranking_version="v2")
    assert missing == 0


# ------------------------------------------------------------- the query-time fallback


async def test_the_v2_sort_falls_back_to_the_v1_score(
    async_session_maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing v2 score must not put a coupon at the top of the listing.

    The unscored coupon has the better v1 score, so with the fallback it
    ranks first. Without the fallback its score is NULL, and the database
    decides what that means: Postgres sorts NULLs first in a descending
    order, which would put an unscored coupon at the top of the storefront.
    """
    monkeypatch.setattr(get_settings(), "ranking_version", "v2", raising=False)

    async with async_session_maker() as db:
        await _coupon(db, title="Unscored", quality_score=99)
        await _coupon(db, title="Scored", quality_score=5, quality_score_v2=50)

        rows, _total = await list_coupons(db, sort=CouponSort.recommended, limit=10)

    assert [c.title for c in rows] == ["Unscored", "Scored"]


def test_the_v2_expression_reads_both_columns() -> None:
    """The fallback is in the sort key itself, not in the order it is applied."""
    from sqlalchemy.dialects import postgresql

    sql = str(
        ranking.ranking_score("v2").compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "coalesce" in sql.lower()
    assert "quality_score_v2" in sql
    assert "quality_score" in sql

    plain = str(
        ranking.ranking_score("v1").compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "coalesce" not in plain.lower()
    assert "quality_score_v2" not in plain


async def test_the_v1_sort_is_unchanged(
    async_session_maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "ranking_version", "v1", raising=False)

    async with async_session_maker() as db:
        await _coupon(db, title="Low v1", quality_score=10)
        await _coupon(db, title="High v1", quality_score=90)
        await _coupon(db, title="Has v2", quality_score=5, quality_score_v2=95)

        rows, _total = await list_coupons(db, sort=CouponSort.recommended, limit=10)

    assert [c.title for c in rows] == ["High v1", "Low v1", "Has v2"]


def test_both_ranking_readers_use_the_same_expression() -> None:
    """The listing and the exploration sampler must not drift apart.

    They used to pick the score column independently, which is how a fallback
    could be added to one and forgotten in the other.
    """
    import inspect

    from app.services import coupon_service

    source = inspect.getsource(coupon_service)
    assert source.count("ranking.ranking_score(") == 2
