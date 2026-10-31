"""End-to-end tests of the ingestion pipeline against the in-memory SQLite DB."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.ingestion import merchants, pipeline, probe
from app.ingestion.lifecycle import expire_stale_coupons
from app.ingestion.normalize import NormalizeError, normalize_offer, offer_from_dict, parse_discount
from app.ingestion.base import RawOffer
from app.models.category import Category
from app.models.coupon import Coupon, CouponStatus
from app.models.coupon_source import CouponSource, CouponSourceStatus
from app.models.ingestion_run import IngestionRun, RunStatus
from app.models.source import Source, SourceKind
from app.models.store import Store

pytestmark = pytest.mark.asyncio


async def test_provider_category_alias_resolves_to_existing_site_category(async_session_maker):
    async with async_session_maker() as db:
        category = Category(name="Electronics", slug="electronics")
        source = Source(
            name="Admitad taxonomy",
            slug="admitad-taxonomy",
            kind=SourceKind.admitad,
            config={},
        )
        db.add_all([category, source])
        await db.commit()

        offer = normalize_offer(
            RawOffer(
                title="A laptop coupon",
                store_name="Laptop shop",
                category_name="Computers & Electronics",
                destination_url="https://shop.example/laptop",
            )
        )
        resolved = await pipeline._resolve_category(db, offer, source)

        assert resolved is not None
        assert resolved.id == category.id
        assert await db.scalar(select(Category).where(Category.slug == "computers-electronics")) is None


async def test_rejected_offer_marks_run_partial_and_preserves_existing_source_link(
    async_session_maker, category
):
    async with async_session_maker() as db:
        source = Source(
            name="Changing taxonomy",
            slug="changing-taxonomy",
            kind=SourceKind.static,
            config={
                "offers": [
                    {
                        "title": "Valid deal",
                        "store_slug": "sample-shop",
                        "category_slug": "fashion",
                        "destination_url": "https://sample-shop.example/deal",
                        "external_id": "valid-1",
                    }
                ]
            },
        )
        db.add(source)
        await db.commit()
        await db.refresh(source)

        initial = await pipeline.run_ingestion(db, source)
        assert initial.status is RunStatus.success
        coupon_source = (
            await db.execute(select(CouponSource).where(CouponSource.source_id == source.id))
        ).scalar_one()

        source.config = {
            "offers": [
                {
                    "title": "Provider category drift",
                    "store_slug": "sample-shop",
                    "category_slug": "not-in-our-taxonomy",
                    "destination_url": "https://sample-shop.example/other",
                    "external_id": "new-2",
                }
            ]
        }
        await db.commit()

        run = await pipeline.run_ingestion(db, source)
        await db.refresh(coupon_source)

        assert run.status is RunStatus.partial
        assert run.failed == 1
        assert "existing source links were preserved" in (run.error or "")
        assert coupon_source.status is CouponSourceStatus.active


@pytest.fixture
async def category(async_session_maker):
    async with async_session_maker() as db:
        cat = Category(name="Fashion", slug="fashion")
        db.add(cat)
        await db.commit()
        await db.refresh(cat)
        return cat


def _offer(**overrides):
    base = {
        "title": "Flat 50% Off on Kurtas",
        "store_slug": "myntra",
        "store_name": "Myntra",
        "category_slug": "fashion",
        "code": "kurt a50",
        "discount_text": "50% off",
        "destination_url": "https://www.myntra.com/offers/kurta?deal=1",
        "expires_at": (datetime.now(UTC) + timedelta(days=7)).strftime("%Y-%m-%d"),
        "external_id": "offer-123",
    }
    base.update(overrides)
    # codes in tests are upper-cased by normalize; normalize also strips spaces,
    # but what we really want is a code WITHOUT spaces, so fix the default.
    if base.get("code") == "kurt a50":
        base["code"] = "KURTA50"
    return offer_from_dict(base)


@pytest.fixture
async def static_source(async_session_maker, category):
    offers = [
        {
            "title": "Flat 50% Off on Kurtas",
            "store_slug": "myntra",
            "store_name": "Myntra",
            "category_slug": "fashion",
            "code": "KURTA50",
            "discount_text": "50% off",
            "destination_url": "https://www.myntra.com/offers/kurta",
            "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
            "external_id": "offer-123",
        },
        {
            "title": "Free shipping on orders above 499",
            "store_slug": "myntra",
            "category_slug": "fashion",
            "discount_text": "free shipping",
            "destination_url": "https://www.myntra.com/shipping",
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            "external_id": "offer-124",
        },
    ]
    async with async_session_maker() as db:
        source = Source(
            name="Seed",
            slug="seed",
            kind=SourceKind.static,
            config={"offers": offers},
            is_enabled=True,
        )
        db.add(source)
        await db.commit()
        await db.refresh(source)
        return source


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The registrable host, not the TLD. This is the whole point of the
        # function: `stores.normalized_domain` is UNIQUE, so two `.com` retailers
        # normalizing to `"com"` would be merged onto one merchant row.
        ("https://www.myntra.com/offers/kurta?deal=1", "myntra.com"),
        ("www.nike.com", "nike.com"),
        ("myntra.com", "myntra.com"),
        ("m.nike.com", "nike.com"),
        ("MYNTRA.COM", "myntra.com"),
        ("myntra.com:8080", "myntra.com"),
        # Multi-label public suffixes keep the label in front of them.
        ("https://www.amazon.co.uk/", "amazon.co.uk"),
        ("https://shop.example.com.au/x", "example.com.au"),
        # Subdomains that are not a storefront front door stay put.
        ("sub.domain.example.org", "example.org"),
        # A host that *is* a public suffix names no merchant.
        ("co.uk", None),
        ("https://co.uk", None),
        # No dot, no guessing. Inventing a domain is how unrelated shops merge.
        ("localhost", None),
        ("example", None),
        ("", None),
        (None, None),
    ],
)
async def test_normalize_domain_returns_the_registrable_host(raw, expected):
    assert merchants.normalize_domain(raw) == expected


async def test_parse_discount():
    from app.models.coupon import DiscountType

    assert parse_discount("50% off") == (DiscountType.percentage, 50.0)
    assert parse_discount("₹200 off on first order") == (DiscountType.fixed, 200.0)
    assert parse_discount("BOGO on tees") == (DiscountType.deal, None)
    assert parse_discount("") is None


async def test_normalize_rejects_bad_offer():
    raw = _offer(destination_url="", title="")
    with pytest.raises(NormalizeError):
        normalize_offer(raw)


async def test_pipeline_creates_coupons_and_run(async_session_maker, static_source):
    async with async_session_maker() as db:
        run = await pipeline.run_ingestion(db, static_source)
        assert run.status == RunStatus.success
        assert run.fetched == 2
        assert run.created == 2

        coupons = (await db.execute(select(Coupon))).scalars().all()
        assert len(coupons) == 2
        assert all(c.status == CouponStatus.active for c in coupons)
        assert all(c.is_active for c in coupons)
        myntra = (await db.execute(select(Store).where(Store.slug == "myntra"))).scalars().one()
        assert all(c.store_id == myntra.id for c in coupons)


async def test_pipeline_dedupes_on_second_run(async_session_maker, static_source):
    async with async_session_maker() as db:
        await pipeline.run_ingestion(db, static_source)
        run2 = await pipeline.run_ingestion(db, static_source)
        assert run2.created == 0
        assert run2.updated == 2
        assert len((await db.execute(select(Coupon))).scalars().all()) == 2


async def test_pipeline_marks_expired_offers(async_session_maker, category):
    async with async_session_maker() as db:
        source = Source(
            name="Old",
            slug="old",
            kind=SourceKind.static,
            config={
                "offers": [
                    {
                        "title": "Expired deal",
                        "store_slug": "flipkart",
                        "category_slug": "fashion",
                        "discount_text": "10% off",
                        "destination_url": "https://www.flipkart.com/x",
                        "expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                    }
                ]
            },
        )
        db.add(source)
        await db.commit()
        await db.refresh(source)
        run = await pipeline.run_ingestion(db, source)
        assert run.created == 1
        coupon = (await db.execute(select(Coupon))).scalars().one()
        assert coupon.status == CouponStatus.expired
        assert coupon.is_active is False
        assert coupon.failure_reason == "expired"


async def test_pipeline_failed_probe_marks_offer_failed(async_session_maker, category, monkeypatch):
    async def fake_probe(url: str):
        return False, "destination returned HTTP 404"

    monkeypatch.setattr(probe, "probe_destination", fake_probe)
    monkeypatch.setattr(pipeline.probe, "probe_destination", fake_probe)

    async with async_session_maker() as db:
        source = Source(
            name="Probed",
            slug="probed",
            kind=SourceKind.static,
            probe_destinations=True,
            config={
                "offers": [
                    {
                        "title": "Some deal",
                        "store_slug": "ajio",
                        "category_slug": "fashion",
                        "discount_text": "20% off",
                        "destination_url": "https://www.ajio.com/dead",
                    }
                ]
            },
        )
        db.add(source)
        await db.commit()
        await db.refresh(source)
        await pipeline.run_ingestion(db, source)
        coupon = (await db.execute(select(Coupon))).scalars().one()
        assert coupon.status == CouponStatus.failed
        assert coupon.failure_reason == "destination returned HTTP 404"
        assert coupon.last_checked_at is not None


async def test_pipeline_skips_unknown_category(async_session_maker, static_source):
    async with async_session_maker() as db:
        static_source.config = {
            "offers": [
                {
                    "title": "Voucher",
                    "store_slug": "amazon",
                    "category_slug": "nonexistent",
                    "destination_url": "https://www.amazon.in/x",
                }
            ]
        }
        run = await pipeline.run_ingestion(db, static_source)
        assert run.failed == 1
        assert run.created == 0


async def test_expire_stale_coupons(async_session_maker, static_source):
    async with async_session_maker() as db:
        await pipeline.run_ingestion(db, static_source)
        coupon = (await db.execute(select(Coupon).limit(1))).scalars().one()
        coupon.expires_at = datetime.now(UTC) - timedelta(hours=1)
        await db.commit()
        expired = await expire_stale_coupons(db)
        assert expired == 1
        await db.refresh(coupon)
        assert coupon.status == CouponStatus.expired
        assert coupon.is_active is False


async def test_ingestion_run_recorded_on_disabled_source(async_session_maker, static_source):
    async with async_session_maker() as db:
        static_source.is_enabled = False
        await db.commit()
        run = await pipeline.run_ingestion(db, static_source)
        assert run.status == RunStatus.failed
        assert "disabled" in (run.error or "")
        runs = (await db.execute(select(IngestionRun))).scalars().all()
        assert len(runs) >= 1


async def test_content_hash_ignores_discount_changes():
    from app.ingestion import dedupe

    offer_a = normalize_offer(
        offer_from_dict(
            {
                "title": "Sale",
                "store_slug": "x",
                "code": "ABC",
                "discount_text": "10% off",
                "destination_url": "https://x.com",
            }
        )
    )
    offer_b = normalize_offer(
        offer_from_dict(
            {
                "title": "Sale",
                "store_slug": "x",
                "code": "ABC",
                "discount_text": "70% off",
                "destination_url": "https://x.com",
            }
        )
    )
    assert dedupe.content_hash(offer_a) == dedupe.content_hash(offer_b)


async def test_a_partial_run_keeps_the_records_the_provider_did_send(
    async_session_maker, category, monkeypatch
):
    # `PartialFetchError` promises "here is everything I managed to retrieve".
    # The pipeline batched records before consuming them, so the last,
    # not-yet-full batch was only yielded *after* the stream ended — and a stream
    # that raises never gets there. Every provider that truncated mid-page lost up
    # to `BATCH_SIZE - 1` records it had already fetched and paid for.
    from app.ingestion.base import PartialFetchError, RawOffer
    from app.ingestion.registry import _ADAPTERS

    class _TruncatingSource:
        async def stream(self, config, *, mode=None, credentials=None):
            yield RawOffer(
                title="Kept deal",
                store_slug="truncatedshop",
                category_slug="fashion",
                code="KEPT",
                discount_text="10% off",
                destination_url="https://truncatedshop.example.com/deal",
            )
            raise PartialFetchError("failed on page 2 of 40", pages_completed=2)

    monkeypatch.setitem(_ADAPTERS, SourceKind.static, _TruncatingSource)

    async with async_session_maker() as db:
        source = Source(
            name="Truncating",
            slug="truncating",
            kind=SourceKind.static,
            config={},
        )
        db.add(source)
        await db.commit()
        await db.refresh(source)

        run = await pipeline.run_ingestion(db, source)

        assert run.status == RunStatus.partial
        assert run.fetched == 1
        assert run.created == 1
        assert "page 2 of 40" in (run.error or "")
        coupon = (await db.execute(select(Coupon))).scalar_one()
        assert coupon.title == "Kept deal"


async def test_a_merchant_another_run_created_first_is_adopted_not_duplicated(
    async_session_maker, category, monkeypatch
):
    # Two sources ingested independently will meet the same new merchant at the
    # same time, so "SELECT, find nothing, INSERT" racing another run's INSERT is
    # the normal case rather than an exotic one. The loser has to adopt the
    # winner's row; a duplicate `stores` row would split every future coupon for
    # that merchant across two pages.
    #
    # The stale read is simulated rather than raced: `_by_domain` misses once, as
    # it would have done had the rival committed in the microsecond between the
    # two statements, and the row really is already there for the INSERT to
    # collide with.
    from app.ingestion import merchant_resolution

    # The rival spells the merchant differently, so the alias and exact-name steps
    # cannot rescue the stale read and the run really does reach the INSERT. That
    # is the realistic case: providers disagree about spelling far more often than
    # they disagree about a hostname.
    async with async_session_maker() as db:
        rival = Store(
            name="Myntra Fashion",
            slug="myntra-fashion",
            normalized_name="myntra fashion",
            normalized_domain="myntra.com",
        )
        db.add(rival)
        await db.commit()

    real_by_domain = merchant_resolution._by_domain
    seen = {"n": 0}

    async def _stale_first_read(db, domain):
        seen["n"] += 1
        return None if seen["n"] == 1 else await real_by_domain(db, domain)

    monkeypatch.setattr(merchant_resolution, "_by_domain", _stale_first_read)

    async with async_session_maker() as db:
        match = await merchant_resolution.resolve_or_create(
            db, normalize_offer(_offer(store_domain="myntra.com"))
        )
        assert match.matched_by == "domain"
        assert match.is_new is False
        await db.commit()
        # The winner's row, not a second one: two rows would split every future
        # coupon for this merchant across two pages.
        stores = (await db.execute(select(Store))).scalars().all()
        assert [store.id for store in stores] == [match.store.id]


async def test_creating_a_merchant_does_not_expire_the_caller_s_own_objects(
    async_session_maker, category
):
    # The recovery used to be a savepoint, and SQLAlchemy 2.1 marks the enclosing
    # transaction unusable when a flush inside one fails. Recovering therefore
    # needed a full `db.rollback()`, which expired every object in the session —
    # including the pipeline's `Source` and `IngestionRun`, so the run's own
    # counters silently reverted and the next attribute read raised
    # `MissingGreenlet`. Writing the row through `ON CONFLICT` means no flush and
    # no rollback, so nothing is expired; this pins that down.
    from app.ingestion import merchant_resolution

    async with async_session_maker() as db:
        source = Source(name="Solo", slug="solo", kind=SourceKind.static, config={})
        db.add(source)
        await db.commit()

        match = await merchant_resolution.resolve_or_create(
            db, normalize_offer(_offer(store_slug="freshshop", store_name="Fresh Shop"))
        )
        assert match.is_new is True
        # No refresh, no re-select: the point is that the attribute is still loaded.
        assert source.slug == "solo"
        assert match.store.slug == "freshshop"


async def test_two_merchants_without_a_domain_are_not_collapsed(async_session_maker, category):
    # `normalized_domain` is nullable and both databases treat NULLs as distinct,
    # which is what lets a provider that gives a name but no usable domain still
    # get its own row. The `ON CONFLICT (normalized_domain)` in
    # `merchant_resolution._claim_domain` must not narrow that into a constraint on
    # NULL — an index that ignored NULLs would silently merge unrelated shops.
    from app.ingestion import merchant_resolution

    # A provider that lists a shop but gives no usable hostname. `localhost` has no
    # registrable domain, which is exactly the shape of offer that leaves
    # `normalized_domain` NULL.
    offer = normalize_offer(
        _offer(
            store_slug="nameless",
            store_name="Nameless Shop",
            destination_url="https://localhost/deal",
        )
    )
    assert offer.store_domain is None

    async with async_session_maker() as db:
        first = await merchant_resolution.resolve_or_create(db, offer)
        second = await merchant_resolution.resolve_or_create(db, offer)
        await db.commit()

        assert first.is_new is True
        assert first.store.normalized_domain is None
        assert second.store.id == first.store.id
        stores = (await db.execute(select(Store))).scalars().all()
        assert [store.id for store in stores] == [first.store.id]


def test_parse_discount_handles_thousands_separators_and_trailing_currency() -> None:
    from app.ingestion.normalize import parse_discount
    from app.models.coupon import DiscountType

    assert parse_discount("2 000 \u20b9") == (DiscountType.fixed, 2000.0)
    assert parse_discount("2\u00a0000 \u20b9") == (DiscountType.fixed, 2000.0)
    assert parse_discount("\u20b92,000 off") == (DiscountType.fixed, 2000.0)  # was 2.0
    assert parse_discount("Rs. 1,00,000") == (DiscountType.fixed, 100000.0)
    assert parse_discount("500 INR") == (DiscountType.fixed, 500.0)
    assert parse_discount("\u20b919,90") == (DiscountType.fixed, 19.9)  # a decimal comma
    assert parse_discount("Up to 40%") == (DiscountType.percentage, 40.0)


def test_to_float_tells_decimal_commas_from_thousands_commas() -> None:
    from app.ingestion.normalize import _to_float

    assert _to_float("19,90") == 19.9
    assert _to_float("2,000") == 2000.0
    assert _to_float("1,00,000") == 100000.0
    assert _to_float("1.234,56") == 1234.56
    assert _to_float("1,234.56") == 1234.56


async def _network_of(db, store_id) -> str:
    return (
        await db.execute(select(Store.affiliate_network).where(Store.id == store_id))
    ).scalar_one()


async def test_a_new_merchant_claims_the_network_its_offer_came_from(async_session_maker):
    from app.ingestion import merchant_resolution

    async with async_session_maker() as db:
        match = await merchant_resolution.resolve_or_create(
            db,
            normalize_offer(
                _offer(
                    destination_url="https://www.acer.com/in-en/deals",
                    store_slug="acer",
                    store_name="Acer",
                )
            ),
            affiliate_network="admitad",
        )
        await db.commit()
        assert await _network_of(db, match.store.id) == "admitad"


async def test_an_existing_store_with_no_network_takes_it(async_session_maker):
    from app.ingestion import merchant_resolution

    async with async_session_maker() as db:
        db.add(
            Store(
                name="Nykaa", slug="nykaa", normalized_name="nykaa", normalized_domain="nykaa.com"
            )
        )
        await db.commit()
        match = await merchant_resolution.resolve_or_create(
            db,
            normalize_offer(
                _offer(
                    destination_url="https://www.nykaa.com/offers",
                    store_slug="nykaa",
                    store_name="Nykaa",
                )
            ),
            affiliate_network="admitad",
        )
        await db.commit()
        assert match.matched_by == "domain"
        assert await _network_of(db, match.store.id) == "admitad"


async def test_a_store_already_on_another_network_keeps_it(async_session_maker):
    from app.ingestion import merchant_resolution

    async with async_session_maker() as db:
        db.add(
            Store(
                name="Myntra",
                slug="myntra",
                normalized_name="myntra",
                normalized_domain="myntra.com",
                affiliate_network="awin",
            )
        )
        await db.commit()
        match = await merchant_resolution.resolve_or_create(
            db, normalize_offer(_offer(store_domain="myntra.com")), affiliate_network="admitad"
        )
        await db.commit()
        assert await _network_of(db, match.store.id) == "awin"


async def test_resolving_without_a_network_changes_nothing(async_session_maker):
    from app.ingestion import merchant_resolution

    async with async_session_maker() as db:
        match = await merchant_resolution.resolve_or_create(
            db,
            normalize_offer(
                _offer(
                    destination_url="https://www.flipkart.com/offers",
                    store_slug="flipkart",
                    store_name="Flipkart",
                )
            ),
        )
        await db.commit()
        assert await _network_of(db, match.store.id) == "none"
