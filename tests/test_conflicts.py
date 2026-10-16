"""Conflict resolution: what a coupon says when providers disagree.

These test the rules stated in `app/ingestion/conflicts.py`'s module docstring
directly, because the rules are the product decision: earliest expiry wins,
missing never erases known, and the highest-priority source that *actually
supplied* a value wins the field. A regression here silently changes what every
shopper sees.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.ingestion import conflicts
from app.ingestion.normalize import NormalizedOffer
from app.models.coupon import DiscountType
from app.models.coupon_source import CouponSource, CouponSourceStatus
from app.models.source import Source, SourceKind

pytestmark = pytest.mark.asyncio

NOW = datetime.now(UTC)


def _offer(**overrides) -> NormalizedOffer:
    base = {
        "title": "Flat 50% Off",
        "code": "SAVE50",
        "description": None,
        "discount_type": DiscountType.percentage,
        "discount_value": 50.0,
        "currency": "INR",
        "store_slug": "myntra",
        "store_name": "Myntra",
        "store_domain": "myntra.com",
        "store_normalized_name": "myntra",
        "category_slug": "fashion",
        "category_name": "Fashion",
        "destination_url": "https://www.myntra.com/offers",
        "starts_at": None,
        "expires_at": NOW + timedelta(days=7),
        "external_id": "offer-1",
        "country": "IN",
    }
    base.update(overrides)
    return NormalizedOffer(**base)


def _source(slug: str, *, priority: int = 100, confidence: float = 0.9, **overrides) -> Source:
    base = {
        "id": uuid4(),
        "name": slug.title(),
        "slug": slug,
        "kind": SourceKind.static,
        "config": {},
        "is_enabled": True,
        "priority": priority,
        "confidence": confidence,
    }
    base.update(overrides)
    return Source(**base)


def _claim(
    slug: str, offer: NormalizedOffer | None = None, **source_kwargs
) -> conflicts.SourceClaim:
    return conflicts.SourceClaim(source=_source(slug, **source_kwargs), offer=offer or _offer())


# ---- SourceClaim -------------------------------------------------------------


async def test_source_claim_properties_read_through_to_source():
    source = _source("awin", priority=7, confidence=0.75)
    claim = conflicts.SourceClaim(source=source, offer=_offer())

    assert claim.priority == 7
    assert claim.confidence == 0.75
    assert claim.usable is True


async def test_source_claim_usable_boundary_at_threshold():
    at = conflicts.SourceClaim(source=_source("at", confidence=0.1), offer=_offer())
    below = conflicts.SourceClaim(source=_source("below", confidence=0.09), offer=_offer())

    assert at.usable is True
    assert below.usable is False


# ---- resolve() — field-level rules ------------------------------------------


async def test_resolve_requires_at_least_one_claim():
    with pytest.raises(ValueError, match="at least one claim"):
        conflicts.resolve([])


async def test_resolve_single_claim_returns_its_values():
    claim = _claim("solo")
    resolved = conflicts.resolve([claim])

    assert resolved.title == "Flat 50% Off"
    assert resolved.discount_type is DiscountType.percentage
    assert resolved.discount_value == 50.0
    assert resolved.destination_url == "https://www.myntra.com/offers"
    assert resolved.had_conflict is False


async def test_resolve_field_missing_from_every_claim_uses_default():
    offer = _offer(description=None)
    claim = _claim("solo", offer)
    resolved = conflicts.resolve([claim])

    decision = next(d for d in resolved.decisions if d.field_name == "description")
    assert decision.chosen_from is None
    assert decision.reason == "no source supplied this field"


async def test_resolve_higher_priority_source_wins_the_field():
    trusted = _claim("trusted", _offer(title="Trusted title"), priority=1)
    noisy = _claim("noisy", _offer(title="Noisy title"), priority=50)

    resolved = conflicts.resolve([noisy, trusted])

    assert resolved.title == "Trusted title"
    decision = next(d for d in resolved.decisions if d.field_name == "title")
    assert decision.chosen_from == "trusted"
    assert decision.conflict is True
    assert decision.alternatives == {"noisy": "Noisy title"}


async def test_resolve_priority_source_that_omitted_a_value_does_not_blank_it_out():
    """A priority-1 source with no discount must not erase a priority-50 one's."""
    empty = _claim(
        "empty",
        _offer(discount_type=None, discount_value=None, title=""),
        priority=1,
    )
    supplying = _claim("supplying", _offer(title="Supplied title"), priority=50)

    resolved = conflicts.resolve([empty, supplying])

    assert resolved.title == "Supplied title"
    decision = next(d for d in resolved.decisions if d.field_name == "title")
    assert decision.chosen_from == "supplying"


async def test_resolve_below_confidence_threshold_falls_back_to_most_trusted():
    low_a = _claim("low-a", _offer(title="Low A"), priority=1, confidence=0.05)
    low_b = _claim("low-b", _offer(title="Low B"), priority=2, confidence=0.04)

    resolved = conflicts.resolve([low_a, low_b])

    decision = next(d for d in resolved.decisions if d.field_name == "title")
    assert decision.chosen == "Low A"
    assert decision.chosen_from == "low-a"
    assert "confidence threshold" in decision.reason


async def test_resolve_agreeing_sources_are_not_a_conflict():
    a = _claim("a", _offer(title="Same"), priority=1)
    b = _claim("b", _offer(title="Same"), priority=2)

    resolved = conflicts.resolve([a, b])

    decision = next(d for d in resolved.decisions if d.field_name == "title")
    assert decision.conflict is False


async def test_resolve_discount_type_skipped_when_neither_value_nor_type_supplied():
    none_offer = _offer(discount_type=None, discount_value=None)
    other = _claim("other", _offer(discount_type=DiscountType.fixed, discount_value=200.0))
    resolved = conflicts.resolve(
        [conflicts.SourceClaim(source=_source("none"), offer=none_offer), other]
    )

    assert resolved.discount_type is DiscountType.fixed
    assert resolved.discount_value == 200.0


# ---- _hashable ---------------------------------------------------------------


async def test_hashable_rounds_floats_and_passes_others_through():
    assert conflicts._hashable(20.000001) == conflicts._hashable(20.0)
    assert conflicts._hashable("20%") == "20%"
    assert conflicts._hashable(20) == 20


# ---- _ordered -----------------------------------------------------------------


async def test_ordered_puts_most_trusted_first():
    noisy = _claim("noisy", priority=50)
    trusted = _claim("trusted", priority=1)
    mid = _claim("mid", priority=10)

    ordered = conflicts._ordered([noisy, trusted, mid])

    assert [c.source.slug for c in ordered] == ["trusted", "mid", "noisy"]


async def test_ordered_is_total_for_identical_priorities():
    """Equal priority+confidence must not leave order to chance."""
    zebra = _claim("zebra", priority=100, confidence=0.5)
    alpha = _claim("alpha", priority=100, confidence=0.5)

    first = [c.source.slug for c in conflicts._ordered([zebra, alpha])]
    second = [c.source.slug for c in conflicts._ordered([alpha, zebra])]

    assert first == second == ["alpha", "zebra"]


# ---- expiry: earliest among the most-trusted tier ---------------------------


async def test_expiry_no_source_states_one_returns_none():
    claim = _claim("solo", _offer(expires_at=None))
    resolved = conflicts.resolve([claim])

    assert resolved.expires_at is None
    assert not any(d.field_name == "expires_at" for d in resolved.decisions)


async def test_expiry_same_priority_earliest_wins():
    early = NOW + timedelta(days=1)
    late = NOW + timedelta(days=10)
    a = _claim("a", _offer(expires_at=late), priority=1)
    b = _claim("b", _offer(expires_at=early), priority=1)

    resolved = conflicts.resolve([a, b])

    # Same priority tier; the *earliest* date must win regardless of order.
    assert resolved.expires_at is not None
    assert resolved.expires_at.date() == early.date()
    decision = next(d for d in resolved.decisions if d.field_name == "expires_at")
    assert decision.conflict is True
    assert decision.chosen_from == "b"


async def test_expiry_priority_trumps_earliness():
    """The most-trusted tier decides; a lower-priority earlier date loses."""
    trusted_late = NOW + timedelta(days=10)
    noisy_early = NOW + timedelta(days=1)
    trusted = _claim("trusted", _offer(expires_at=trusted_late), priority=1)
    noisy = _claim("noisy", _offer(expires_at=noisy_early), priority=50)

    resolved = conflicts.resolve([trusted, noisy])

    assert resolved.expires_at is not None
    assert resolved.expires_at.date() == trusted_late.date()
    decision = next(d for d in resolved.decisions if d.field_name == "expires_at")
    assert decision.chosen_from == "trusted"


async def test_expiry_missing_never_erases_known():
    known = NOW + timedelta(days=3)
    stating = _claim("stating", _offer(expires_at=known), priority=1)
    silent = _claim("silent", _offer(expires_at=None), priority=2)

    resolved = conflicts.resolve([stating, silent])

    assert resolved.expires_at is not None
    assert resolved.expires_at.date() == known.date()
    # A less-trusted silent source doesn't even make it into the alternatives:
    # only sources at the deciding tier (same or better priority) are recorded.
    decision = next(d for d in resolved.decisions if d.field_name == "expires_at")
    assert decision.alternatives == {}


async def test_expiry_silent_equal_priority_source_recorded_as_not_stated():
    """A peer source (same tier) that stayed silent is recorded as such."""
    known = NOW + timedelta(days=3)
    stating = _claim("stating", _offer(expires_at=known), priority=1)
    silent = _claim("silent", _offer(expires_at=None), priority=1)

    resolved = conflicts.resolve([stating, silent])

    assert resolved.expires_at is not None
    assert resolved.expires_at.date() == known.date()
    decision = next(d for d in resolved.decisions if d.field_name == "expires_at")
    assert decision.alternatives.get("silent") == "not stated"


async def test_expiry_agreement_at_top_tier_is_not_a_conflict():
    when = NOW + timedelta(days=5)
    a = _claim("a", _offer(expires_at=when), priority=1)
    b = _claim("b", _offer(expires_at=when), priority=1)

    resolved = conflicts.resolve([a, b])

    decision = next(d for d in resolved.decisions if d.field_name == "expires_at")
    assert decision.conflict is False


# ---- starts: earliest wins ---------------------------------------------------


async def test_starts_no_source_states_one_returns_none():
    claim = _claim("solo", _offer(starts_at=None))
    resolved = conflicts.resolve([claim])

    assert resolved.starts_at is None


async def test_starts_earliest_wins():
    early = NOW - timedelta(days=2)
    late = NOW + timedelta(days=2)
    a = _claim("a", _offer(starts_at=late), priority=3)
    b = _claim("b", _offer(starts_at=early), priority=3)

    resolved = conflicts.resolve([a, b])

    assert resolved.starts_at is not None
    decision = next(d for d in resolved.decisions if d.field_name == "starts_at")
    assert decision.chosen_from == "b"
    assert decision.conflict is True


async def test_starts_agreement_is_not_a_conflict():
    when = NOW - timedelta(days=1)
    a = _claim("a", _offer(starts_at=when), priority=1)
    b = _claim("b", _offer(starts_at=when), priority=5)

    resolved = conflicts.resolve([a, b])

    decision = next(d for d in resolved.decisions if d.field_name == "starts_at")
    assert decision.conflict is False


# ---- audit trail -------------------------------------------------------------


async def test_had_conflict_true_when_any_decision_conflicted():
    decision = conflicts.ResolutionDecision(
        field_name="title", chosen="x", chosen_from="a", reason="r", conflict=True
    )
    resolved = conflicts.ResolvedCoupon(
        title="x",
        description=None,
        discount_type=DiscountType.percentage,
        discount_value=1.0,
        starts_at=None,
        expires_at=None,
        destination_url="https://x.example",
        decisions=[decision],
    )

    assert resolved.had_conflict is True


async def test_as_audit_shapes_decisions_for_storage():
    decision = conflicts.ResolutionDecision(
        field_name="title",
        chosen="Winner",
        chosen_from="a",
        reason="highest-priority source that supplied a value",
        alternatives={"b": "Loser"},
        conflict=True,
    )
    resolved = conflicts.ResolvedCoupon(
        title="Winner",
        description=None,
        discount_type=DiscountType.percentage,
        discount_value=1.0,
        starts_at=None,
        expires_at=None,
        destination_url="https://x.example",
        decisions=[decision],
    )

    audit = resolved.as_audit()

    assert audit["title"]["chosen"] == "Winner"
    assert audit["title"]["from"] == "a"
    assert audit["title"]["conflict"] is True
    assert audit["title"]["alternatives"] == {"b": "Loser"}


# ---- DB-backed: update_coupon_sources ---------------------------------------


@pytest.fixture
async def world(async_session_maker):
    """A category + store + coupon to hang CouponSource rows off."""
    from app.models.category import Category
    from app.models.coupon import Coupon
    from app.models.store import Store

    async with async_session_maker() as db:
        cat = Category(name="Fashion", slug="fashion")
        store = Store(
            name="Myntra",
            slug="myntra",
            normalized_name="myntra",
            normalized_domain="myntra.com",
        )
        db.add_all([cat, store])
        await db.flush()
        coupon = Coupon(
            title="Flat 50% Off",
            slug="flat-50-off",
            store_id=store.id,
            category_id=cat.id,
            destination_url="https://www.myntra.com/offers",
            discount_type=DiscountType.percentage,
        )
        db.add(coupon)
        await db.commit()
        await db.refresh(cat)
        await db.refresh(store)
        await db.refresh(coupon)
        return {"category": cat, "store": store, "coupon": coupon}


@pytest.fixture
async def db_sources(async_session_maker):
    """Two persisted sources with distinct priorities."""
    async with async_session_maker() as db:
        a = Source(name="Trusted", slug="trusted", kind=SourceKind.static, config={}, priority=1)
        b = Source(name="Noisy", slug="noisy", kind=SourceKind.static, config={}, priority=50)
        db.add_all([a, b])
        await db.commit()
        await db.refresh(a)
        await db.refresh(b)
        return {"trusted": a, "noisy": b}


async def test_update_coupon_sources_inserts_rows(world, db_sources, async_session_maker):
    claims = [
        conflicts.SourceClaim(source=db_sources["trusted"], offer=_offer(title="Trusted title")),
        conflicts.SourceClaim(source=db_sources["noisy"], offer=_offer(title="Noisy title")),
    ]
    async with async_session_maker() as db:
        rows = await conflicts.update_coupon_sources(db, world["coupon"].id, claims)
        await db.commit()

    assert len(rows) == 2
    by_source = {row.source_id: row for row in rows}
    assert by_source[db_sources["trusted"].id].source_title == "Trusted title"
    assert by_source[db_sources["trusted"].id].status is CouponSourceStatus.active
    assert by_source[db_sources["trusted"].id].affiliate_url == "https://www.myntra.com/offers"


async def test_update_coupon_sources_updates_existing_row(world, db_sources, async_session_maker):
    claim = conflicts.SourceClaim(source=db_sources["trusted"], offer=_offer(title="First"))
    async with async_session_maker() as db:
        await conflicts.update_coupon_sources(db, world["coupon"].id, [claim])
        await db.commit()

    updated = conflicts.SourceClaim(source=db_sources["trusted"], offer=_offer(title="Second"))
    async with async_session_maker() as db:
        await conflicts.update_coupon_sources(db, world["coupon"].id, [updated])
        await db.commit()

    from sqlalchemy import func, select

    async with async_session_maker() as db:
        count = await db.scalar(select(func.count()).select_from(CouponSource))
        row = (await db.execute(select(CouponSource))).scalar_one()
    assert count == 1
    assert row.source_title == "Second"


async def test_update_coupon_sources_marks_past_expiry_expired(
    world, db_sources, async_session_maker
):
    claim = conflicts.SourceClaim(
        source=db_sources["trusted"],
        offer=_offer(expires_at=NOW - timedelta(days=1)),
    )
    async with async_session_maker() as db:
        rows = await conflicts.update_coupon_sources(db, world["coupon"].id, [claim])
        await db.commit()

    assert rows[0].status is CouponSourceStatus.expired


async def test_update_coupon_sources_slims_raw_data(world, db_sources, async_session_maker):
    offer = _offer()
    offer.extra = {"tracking": "x" * 10_000, "nested": {"junk": True}}
    claim = conflicts.SourceClaim(source=db_sources["trusted"], offer=offer)
    async with async_session_maker() as db:
        rows = await conflicts.update_coupon_sources(db, world["coupon"].id, [claim])
        await db.commit()

    raw = rows[0].raw_data
    assert set(raw) == {
        "restrictions",
        "source_categories",
        "country",
        "currency",
        "external_id",
    }
    assert "tracking" not in raw


# ---- DB-backed: mark_unseen_sources -----------------------------------------


async def _insert_source_row(db, coupon_id, source_id, *, status=CouponSourceStatus.active):
    row = CouponSource(
        coupon_id=coupon_id,
        source_id=source_id,
        status=status,
        affiliate_url="https://x.example",
    )
    db.add(row)
    await db.commit()
    return row


async def test_mark_unseen_sources_no_ids_is_a_noop(world, db_sources, async_session_maker):
    async with async_session_maker() as db:
        await _insert_source_row(db, world["coupon"].id, db_sources["trusted"].id)
        count = await conflicts.mark_unseen_sources(
            db, world["coupon"].id, [], as_status=CouponSourceStatus.expired
        )
        await db.commit()

    assert count == 0


async def test_mark_unseen_sources_downgrades_only_absent(world, db_sources, async_session_maker):
    async with async_session_maker() as db:
        await _insert_source_row(db, world["coupon"].id, db_sources["trusted"].id)
        await _insert_source_row(db, world["coupon"].id, db_sources["noisy"].id)

        count = await conflicts.mark_unseen_sources(
            db,
            world["coupon"].id,
            [db_sources["trusted"].id],
            as_status=CouponSourceStatus.expired,
        )
        await db.commit()

    assert count == 1
    from sqlalchemy import select

    async with async_session_maker() as db:
        rows = (await db.execute(select(CouponSource))).scalars().all()
    by_source = {row.source_id: row for row in rows}
    assert by_source[db_sources["trusted"].id].status is CouponSourceStatus.active
    assert by_source[db_sources["noisy"].id].status is CouponSourceStatus.expired


async def test_mark_unseen_sources_all_seen_is_a_noop(world, db_sources, async_session_maker):
    async with async_session_maker() as db:
        await _insert_source_row(db, world["coupon"].id, db_sources["trusted"].id)
        count = await conflicts.mark_unseen_sources(
            db,
            world["coupon"].id,
            [db_sources["trusted"].id, db_sources["noisy"].id],
            as_status=CouponSourceStatus.expired,
        )
        await db.commit()

    assert count == 0


async def test_mark_unseen_sources_skips_non_active_rows(world, db_sources, async_session_maker):
    async with async_session_maker() as db:
        await _insert_source_row(
            db, world["coupon"].id, db_sources["trusted"].id, status=CouponSourceStatus.expired
        )
        count = await conflicts.mark_unseen_sources(
            db, world["coupon"].id, [], as_status=CouponSourceStatus.deleted
        )
        await db.commit()

    assert count == 0


# ---- DB-backed: mark_stale_for_source ---------------------------------------


async def test_mark_stale_downgrades_rows_seen_before_run(world, db_sources, async_session_maker):
    run_started = datetime.now(UTC)
    async with async_session_maker() as db:
        row = await _insert_source_row(db, world["coupon"].id, db_sources["trusted"].id)
        row.last_seen_at = run_started - timedelta(hours=1)
        await db.commit()

    async with async_session_maker() as db:
        count = await conflicts.mark_stale_for_source(
            db,
            db_sources["trusted"].id,
            run_started_at=run_started,
            as_status=CouponSourceStatus.expired,
        )
        await db.commit()

    assert count == 1


async def test_mark_stale_spares_rows_seen_after_run_started(
    world, db_sources, async_session_maker
):
    run_started = datetime.now(UTC)
    async with async_session_maker() as db:
        row = await _insert_source_row(db, world["coupon"].id, db_sources["trusted"].id)
        row.last_seen_at = run_started + timedelta(minutes=1)
        await db.commit()

    async with async_session_maker() as db:
        count = await conflicts.mark_stale_for_source(
            db,
            db_sources["trusted"].id,
            run_started_at=run_started,
            as_status=CouponSourceStatus.expired,
        )
        await db.commit()

    assert count == 0


async def test_mark_stale_treats_null_last_seen_as_stale(world, db_sources, async_session_maker):
    async with async_session_maker() as db:
        await _insert_source_row(db, world["coupon"].id, db_sources["trusted"].id)

    async with async_session_maker() as db:
        count = await conflicts.mark_stale_for_source(
            db,
            db_sources["trusted"].id,
            run_started_at=datetime.now(UTC),
            as_status=CouponSourceStatus.expired,
        )
        await db.commit()

    assert count == 1


async def test_mark_stale_does_not_touch_other_sources(world, db_sources, async_session_maker):
    run_started = datetime.now(UTC)
    async with async_session_maker() as db:
        row = await _insert_source_row(db, world["coupon"].id, db_sources["noisy"].id)
        row.last_seen_at = run_started - timedelta(hours=1)
        await db.commit()

    async with async_session_maker() as db:
        count = await conflicts.mark_stale_for_source(
            db,
            db_sources["trusted"].id,
            run_started_at=run_started,
            as_status=CouponSourceStatus.expired,
        )
        await db.commit()

    assert count == 0


async def test_mark_stale_skips_non_active_rows(world, db_sources, async_session_maker):
    run_started = datetime.now(UTC)
    async with async_session_maker() as db:
        row = await _insert_source_row(
            db, world["coupon"].id, db_sources["trusted"].id, status=CouponSourceStatus.expired
        )
        row.last_seen_at = run_started - timedelta(hours=1)
        await db.commit()

    async with async_session_maker() as db:
        count = await conflicts.mark_stale_for_source(
            db,
            db_sources["trusted"].id,
            run_started_at=run_started,
            as_status=CouponSourceStatus.deleted,
        )
        await db.commit()

    assert count == 0


# ---- DB-backed: load_stored_claims ------------------------------------------


async def test_load_stored_claims_returns_all_by_default(world, db_sources, async_session_maker):
    async with async_session_maker() as db:
        await _insert_source_row(db, world["coupon"].id, db_sources["trusted"].id)
        await _insert_source_row(
            db, world["coupon"].id, db_sources["noisy"].id, status=CouponSourceStatus.expired
        )

    async with async_session_maker() as db:
        claims = await conflicts.load_stored_claims(db, world["coupon"].id)

    assert {c.source.slug for c in claims} == {"trusted", "noisy"}
    # Most-trusted first.
    assert claims[0].source.slug == "trusted"


async def test_load_stored_claims_only_filter(world, db_sources, async_session_maker):
    async with async_session_maker() as db:
        await _insert_source_row(db, world["coupon"].id, db_sources["trusted"].id)
        await _insert_source_row(
            db, world["coupon"].id, db_sources["noisy"].id, status=CouponSourceStatus.expired
        )

    async with async_session_maker() as db:
        claims = await conflicts.load_stored_claims(
            db, world["coupon"].id, only=(CouponSourceStatus.expired,)
        )

    assert [c.source.slug for c in claims] == ["noisy"]
    assert claims[0].status is CouponSourceStatus.expired


async def test_load_stored_claims_empty_for_unknown_coupon(async_session_maker):
    async with async_session_maker() as db:
        claims = await conflicts.load_stored_claims(db, uuid4())

    assert claims == []


# ---- resolve_from_stored -----------------------------------------------------


def _stored(slug: str, *, expires=None, url=None, priority=100) -> conflicts.StoredClaim:
    return conflicts.StoredClaim(
        source=_source(slug, priority=priority),
        source_expires_at=expires,
        affiliate_url=url,
        status=CouponSourceStatus.active,
    )


async def test_resolve_from_stored_incoming_expiry_used_when_no_stored():
    incoming = NOW + timedelta(days=2)
    expires, destination, overridden = conflicts.resolve_from_stored(
        [],
        incoming_expires_at=incoming,
        incoming_destination="https://incoming.example",
        incoming_source=_source("incoming"),
    )

    assert expires is not None and expires.date() == incoming.date()
    assert destination == "https://incoming.example"
    assert overridden == []


async def test_resolve_from_stored_missing_incoming_never_erases_stored():
    stored_when = NOW + timedelta(days=4)
    expires, _, _ = conflicts.resolve_from_stored(
        [_stored("stored", expires=stored_when)],
        incoming_expires_at=None,
        incoming_destination="https://incoming.example",
        incoming_source=_source("incoming"),
    )

    assert expires is not None and expires.date() == stored_when.date()


async def test_resolve_from_stored_priority_beats_earliness():
    trusted_late = NOW + timedelta(days=30)
    noisy_early = NOW + timedelta(days=1)
    expires, _, _ = conflicts.resolve_from_stored(
        [_stored("noisy", expires=noisy_early, priority=50)],
        incoming_expires_at=trusted_late,
        incoming_destination="https://incoming.example",
        incoming_source=_source("trusted", priority=1),
    )

    assert expires is not None and expires.date() == trusted_late.date()


async def test_resolve_from_stored_same_priority_earliest_wins():
    stored_early = NOW + timedelta(days=1)
    incoming_late = NOW + timedelta(days=10)
    expires, _, _ = conflicts.resolve_from_stored(
        [_stored("stored", expires=stored_early, priority=1)],
        incoming_expires_at=incoming_late,
        incoming_destination="https://incoming.example",
        incoming_source=_source("incoming", priority=1),
    )

    assert expires is not None and expires.date() == stored_early.date()


async def test_resolve_from_stored_naive_stored_datetime_does_not_crash():
    """SQLite hands back naive datetimes; min() must not raise on the mix."""
    naive_stored = (NOW + timedelta(days=5)).replace(tzinfo=None)
    aware_incoming = NOW + timedelta(days=1)

    expires, _, _ = conflicts.resolve_from_stored(
        [_stored("stored", expires=naive_stored, priority=1)],
        incoming_expires_at=aware_incoming,
        incoming_destination="https://incoming.example",
        incoming_source=_source("incoming", priority=1),
    )

    assert expires is not None


async def test_resolve_from_stored_destination_stored_wins_when_trusted():
    _, destination, overridden = conflicts.resolve_from_stored(
        [_stored("trusted", url="https://stored.example", priority=1)],
        incoming_expires_at=None,
        incoming_destination="https://incoming.example",
        incoming_source=_source("noisy", priority=50),
    )

    assert destination == "https://stored.example"
    assert overridden == ["noisy"]


async def test_resolve_from_stored_destination_incoming_wins_when_trusted():
    _, destination, overridden = conflicts.resolve_from_stored(
        [_stored("noisy", url="https://stored.example", priority=50)],
        incoming_expires_at=None,
        incoming_destination="https://incoming.example",
        incoming_source=_source("trusted", priority=1),
    )

    assert destination == "https://incoming.example"
    assert overridden == ["noisy"]


async def test_resolve_from_stored_destination_falls_back_when_none_have_urls():
    _, destination, overridden = conflicts.resolve_from_stored(
        [_stored("stored", url=None)],
        incoming_expires_at=None,
        incoming_destination="https://incoming.example",
        incoming_source=_source("incoming"),
    )

    assert destination == "https://incoming.example"
    assert overridden == []


async def test_resolve_from_stored_lists_every_overridden_slug():
    _, _, overridden = conflicts.resolve_from_stored(
        [
            _stored("a", url="https://a.example", priority=50),
            _stored("b", url="https://b.example", priority=60),
        ],
        incoming_expires_at=None,
        incoming_destination="https://incoming.example",
        incoming_source=_source("trusted", priority=1),
    )

    assert sorted(overridden) == ["a", "b"]
