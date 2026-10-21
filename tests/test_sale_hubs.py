import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.services import sale_service


async def test_unique_series_slug(async_session_maker):
    async with async_session_maker() as db:
        await sale_service.create_series(db, slug="diwali", title="Diwali")
        with pytest.raises(IntegrityError):
            await sale_service.create_series(db, slug="diwali", title="Diwali 2")


async def test_lifecycle_transition_and_override(async_session_maker):
    async with async_session_maker() as db:
        series = await sale_service.create_series(db, slug="diwali", title="Diwali")
        now = datetime.now(UTC)
        edition = await sale_service.create_edition(
            db,
            series=series,
            slug="diwali-2026",
            year=2026,
            start_at=now + timedelta(days=1),
            end_at=now + timedelta(days=2),
            dates_confirmed=True,
            source_url="https://example.com",
        )
        assert sale_service.transition_lifecycle(edition, now) == "upcoming"
        edition.manual_override = True
        edition.lifecycle_status = "live"
        await db.commit()
        assert sale_service.transition_lifecycle(edition, now + timedelta(days=10)) == "live"


async def test_current_edition_rule(async_session_maker):
    async with async_session_maker() as db:
        series = await sale_service.create_series(db, slug="diwali2", title="Diwali2")
        now = datetime.now(UTC)
        e1 = await sale_service.create_edition(
            db,
            series=series,
            slug="a",
            year=2026,
            start_at=now - timedelta(days=1),
            end_at=now - timedelta(hours=1),
            dates_confirmed=True,
            source_url="https://x",
        )
        e1.publication_status = "published"
        e1.lifecycle_status = "ended"
        e2 = await sale_service.create_edition(
            db,
            series=series,
            slug="b",
            year=2026,
            start_at=now + timedelta(days=1),
            dates_confirmed=True,
            source_url="https://x",
        )
        e2.publication_status = "published"
        e2.lifecycle_status = "upcoming"
        await db.commit()
        chosen = await sale_service.current_edition(db, series)
        assert chosen is not None
        assert chosen.slug == "b"


async def test_content_ref_format():
    assert sale_service.validate_content_ref("sale:diwali:diwali-2026") is True
    assert sale_service.validate_content_ref("sale:Bad Value") is False


async def _store(async_session_maker, slug: str):
    from app.models.store import Store

    async with async_session_maker() as db:
        store = Store(name=f"Store {slug}", slug=slug, website_url=f"https://{slug}.example")
        db.add(store)
        await db.commit()
        await db.refresh(store)
        return store


async def _coupon(async_session_maker, store, category, *, title: str, active: bool = True):
    from app.models.coupon import Coupon, DiscountType

    async with async_session_maker() as db:
        coupon = Coupon(
            title=title,
            slug=f"{title.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
            code="SAVE10",
            discount_type=DiscountType.percentage,
            discount_value=10,
            store_id=store.id,
            category_id=category.id,
            destination_url="https://example.com/deal",
            is_active=active,
        )
        db.add(coupon)
        await db.commit()
        await db.refresh(coupon)
        return coupon


async def test_edition_coupons_resolves_store_and_category_rules(async_session_maker):
    from app.models.category import Category

    async with async_session_maker() as db:
        category = Category(name="Diwali", slug="diwali")
        db.add(category)
        await db.commit()
        await db.refresh(category)

    store = await _store(async_session_maker, "croma")
    other = await _store(async_session_maker, "ajio")
    await _coupon(async_session_maker, store, category, title="Croma Diwali")
    await _coupon(async_session_maker, other, category, title="Ajio Diwali")
    await _coupon(async_session_maker, store, category, title="Croma Expired", active=False)

    async with async_session_maker() as db:
        series = await sale_service.create_series(db, slug="diwali-hub", title="Diwali")
        edition = await sale_service.create_edition(
            db, series=series, slug="diwali-2026", year=2026
        )
        await sale_service.add_rule(db, edition, "store", "croma")
        coupons = await sale_service.edition_coupons(db, edition)

    assert [c.title for c in coupons] == ["Croma Diwali"]


async def test_edition_coupons_ignores_unknown_slugs_and_tags(async_session_maker):
    async with async_session_maker() as db:
        series = await sale_service.create_series(db, slug="empty-hub", title="Empty")
        edition = await sale_service.create_edition(db, series=series, slug="empty-2026", year=2026)
        await sale_service.add_rule(db, edition, "store", "does-not-exist")
        await sale_service.add_rule(db, edition, "tag", "diwali-sale")
        assert await sale_service.edition_coupons(db, edition) == []


async def test_sale_hubs_are_registered_in_the_admin():
    from app.admin.views import ALL_VIEWS

    names = {view.__name__ for view in ALL_VIEWS}
    assert {"SaleSeriesAdmin", "SaleEditionAdmin", "SaleEditionCouponRuleAdmin"} <= names


async def test_the_sales_api_returns_the_coupons_of_an_edition(client, async_session_maker):
    from app.models.category import Category

    async with async_session_maker() as db:
        category = Category(name="Diwali Cat", slug="diwali-cat")
        db.add(category)
        await db.commit()
        await db.refresh(category)

    store = await _store(async_session_maker, "croma")
    await _coupon(async_session_maker, store, category, title="Croma Deal")

    async with async_session_maker() as db:
        series = await sale_service.create_series(db, slug="hub", title="Hub")
        edition = await sale_service.create_edition(db, series=series, slug="hub-2026", year=2026)
        await sale_service.add_rule(db, edition, "store", "croma")
        edition.publication_status = "published"
        edition.lifecycle_status = "live"
        series.publication_status = "published"
        await db.commit()

    resp = await client.get("/api/v1/sales/hub")
    assert resp.status_code == 200
    body = resp.json()
    assert body["edition"] == "hub-2026"
    assert [c["title"] for c in body["coupons"]] == ["Croma Deal"]
