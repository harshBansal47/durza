"""Category resolution: an offer is never dropped over its category wording."""

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.ingestion import pipeline
from app.ingestion.categories import (
    CategoryResolver,
    label_keys,
    normalize_label,
    recategorize_fallback,
    refile_coupons,
    rule_scores,
)
from app.ingestion.sources.admitad import _category_names
from app.models.category import Category
from app.models.category_alias import CategoryAlias
from app.models.coupon import Coupon, DiscountType
from app.models.source import Source, SourceKind
from app.models.store import Store

pytestmark = pytest.mark.asyncio

# The taxonomy that existed before any provider was connected.
GENERIC = [
    ("Beauty", "beauty"),
    ("E-commerce", "e-commerce"),
    ("Electronics", "electronics"),
    ("Fashion", "fashion"),
    ("Food Delivery", "food-delivery"),
    ("Grocery", "grocery"),
    ("Travel", "travel"),
]

# The wording from the real problem report, all of which should land in Electronics.
ELECTRONICS_WORDING = [
    "Computers",
    "Electronics & COMPUTERS ACCESORIES",
    "Computer Accessories",
    "Home Appliances",
    "Consumer Electronics",
]


@pytest.fixture
async def taxonomy(async_session_maker):
    async with async_session_maker() as db:
        db.add_all([Category(name=name, slug=slug) for name, slug in GENERIC])
        await db.commit()


async def _slug(db, category_id) -> str:
    return (await db.execute(select(Category.slug).where(Category.id == category_id))).scalar_one()


async def _alias(db, key: str) -> CategoryAlias | None:
    return await db.scalar(select(CategoryAlias).where(CategoryAlias.alias == key))


# --- wording ------------------------------------------------------------------


async def test_wording_is_compared_without_case_punctuation_or_ampersands():
    assert (
        normalize_label("Electronics & COMPUTERS ACCESORIES")
        == "electronics and computers accesories"
    )
    assert normalize_label("Men's  Wear / Shoes") == "mens wear shoes"
    assert normalize_label("  ") == ""


async def test_a_path_yields_each_level_and_noise_is_dropped():
    keys = [key for key, _ in label_keys(["Computers > Laptops", "computers", "Other", "", None])]
    assert keys == ["computers laptops", "computers", "laptops"]


async def test_a_longer_phrase_outscores_a_bare_word():
    (score, _order, rule), *_rest = rule_scores("home appliances")
    assert (rule.name, score) == ("home-kitchen", 2)  # "home appliance" beats "appliance"


# --- the reported problem ------------------------------------------------------


@pytest.mark.parametrize("wording", ELECTRONICS_WORDING)
async def test_the_reported_wording_files_under_electronics(async_session_maker, taxonomy, wording):
    async with async_session_maker() as db:
        resolution = await CategoryResolver().resolve_labels(db, [wording])
        assert resolution.slug == "electronics"
        assert resolution.method in {"rule", "name"}


async def test_an_offer_with_several_wordings_is_filed_once_by_the_best_of_them(
    async_session_maker, taxonomy
):
    async with async_session_maker() as db:
        resolution = await CategoryResolver().resolve_labels(
            db, ["Zzz Unrecognisable", "Consumer Electronics", "Computer Accessories"]
        )
        assert resolution.slug == "electronics"


async def test_a_more_specific_category_is_used_once_it_exists(async_session_maker, taxonomy):
    async with async_session_maker() as db:
        before = await CategoryResolver().resolve_labels(db, ["Home Appliances"])
        assert before.slug == "electronics"  # no Home & Kitchen yet

        db.add(Category(name="Home & Kitchen", slug="home-kitchen"))
        await db.commit()
        after = await CategoryResolver().resolve_labels(db, ["Home Appliances"])
        assert after.slug == "home-kitchen"


# --- never reject ---------------------------------------------------------------


async def test_unrecognised_wording_is_filed_in_the_fallback_and_queued_for_review(
    async_session_maker, taxonomy
):
    async with async_session_maker() as db:
        resolver = CategoryResolver()
        resolution = await resolver.resolve_labels(db, ["Quantum Widgets"], source_slug="admitad")
        await resolver.flush(db)
        await db.commit()

        assert (resolution.slug, resolution.method) == ("other", "fallback")
        alias = await _alias(db, "quantum widgets")
        assert (alias.method, alias.category_id, alias.seen_count, alias.last_source) == (
            "pending", None, 1, "admitad",
        )  # fmt: skip
        assert resolver.new_pending == ["Quantum Widgets"]


async def test_an_offer_with_no_category_wording_at_all_is_still_filed(
    async_session_maker, taxonomy
):
    async with async_session_maker() as db:
        resolution = await CategoryResolver().resolve_labels(db, [None, "", "Other"])
        assert (resolution.slug, resolution.method) == ("other", "fallback")


async def test_the_source_default_beats_the_fallback_but_not_real_evidence(
    async_session_maker, taxonomy
):
    async with async_session_maker() as db:
        resolver = CategoryResolver()
        assert (
            await resolver.resolve_labels(db, ["Zzz"], default_slug="travel")
        ).method == "default"
        assert (
            await resolver.resolve_labels(db, ["Laptops"], default_slug="travel")
        ).slug == "electronics"


async def test_the_offers_own_slug_wins_when_the_provider_speaks_our_taxonomy(
    async_session_maker, taxonomy
):
    async with async_session_maker() as db:
        resolution = await CategoryResolver().resolve_labels(
            db, ["Laptops"], category_slug="fashion"
        )
        assert (resolution.slug, resolution.method) == ("fashion", "slug")


async def test_an_exact_category_name_matches_without_creating_an_alias(
    async_session_maker, taxonomy
):
    async with async_session_maker() as db:
        resolver = CategoryResolver()
        resolution = await resolver.resolve_labels(db, ["food delivery"])
        await resolver.flush(db)
        await db.commit()
        assert (resolution.slug, resolution.method) == ("food-delivery", "name")
        assert await _alias(db, "food delivery") is None


# --- learning ---------------------------------------------------------------------


async def test_a_persons_choice_beats_the_rules(async_session_maker, taxonomy):
    async with async_session_maker() as db:
        beauty = await db.scalar(select(Category).where(Category.slug == "beauty"))
        db.add(
            CategoryAlias(
                alias="computers", label="Computers", category_id=beauty.id, method="manual"
            )
        )
        await db.commit()

        resolution = await CategoryResolver().resolve_labels(db, ["Computers"])
        assert (resolution.slug, resolution.method) == ("beauty", "alias")


async def test_rule_answers_are_recorded_so_a_wrong_guess_can_be_overridden(
    async_session_maker, taxonomy
):
    async with async_session_maker() as db:
        resolver = CategoryResolver()
        await resolver.resolve_labels(db, ["Computers"], source_slug="admitad")
        await resolver.resolve_labels(db, ["Computers"], source_slug="admitad")
        await resolver.flush(db)
        await db.commit()

        alias = await _alias(db, "computers")
        assert (alias.method, alias.seen_count) == ("rule", 2)
        assert await _slug(db, alias.category_id) == "electronics"


async def test_a_wording_that_was_pending_is_upgraded_once_a_rule_can_place_it(
    async_session_maker, taxonomy
):
    async with async_session_maker() as db:
        db.add(CategoryAlias(alias="home appliances", label="Home Appliances", method="pending"))
        await db.commit()

        resolver = CategoryResolver()
        assert (await resolver.resolve_labels(db, ["Home Appliances"])).slug == "electronics"
        await resolver.flush(db)
        await db.commit()

        alias = await _alias(db, "home appliances")
        assert alias.method == "rule" and await _slug(db, alias.category_id) == "electronics"


async def test_two_runs_meeting_the_same_new_wording_do_not_collide(async_session_maker, taxonomy):
    """Sources run concurrently; a caught IntegrityError would poison the session."""
    # Seed the fallback before opening concurrent transactions. In production
    # the migration does this; this test is specifically about alias upserts.
    async with async_session_maker() as db:
        db.add(Category(name="Other", slug="other"))
        await db.commit()

    async with async_session_maker() as first_db, async_session_maker() as second_db:
        first, second = CategoryResolver(), CategoryResolver()
        await first.resolve_labels(first_db, ["Brand New Wording"], source_slug="a")
        await second.resolve_labels(second_db, ["Brand New Wording"], source_slug="b")
        await first.flush(first_db)
        await first_db.commit()
        await second.flush(second_db)  # the row now exists: must count, not raise
        await second_db.commit()

    async with async_session_maker() as db:
        rows = (
            (
                await db.execute(
                    select(CategoryAlias).where(CategoryAlias.alias == "brand new wording")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1 and rows[0].seen_count == 2


# --- moving what is already filed --------------------------------------------------


async def _coupon(db, store, category, **kw) -> Coupon:
    coupon = Coupon(
        title=kw.pop("title", "An offer"),
        slug=f"c-{uuid.uuid4().hex[:10]}",
        code=f"C{uuid.uuid4().hex[:6].upper()}",
        store_id=store.id,
        category_id=category.id,
        discount_type=DiscountType.percentage,
        destination_url="https://example.com",
        **kw,
    )
    db.add(coupon)
    await db.commit()
    return coupon


async def _parked(db):
    store = Store(name="Shop", slug=f"shop-{uuid.uuid4().hex[:6]}")
    db.add(store)
    await db.commit()
    resolver = CategoryResolver()
    await resolver.resolve_labels(db, ["zzz"])  # creates the fallback
    await db.commit()
    other = await db.scalar(select(Category).where(Category.slug == "other"))
    return store, other


async def test_mapping_a_wording_moves_the_offers_already_waiting_in_the_fallback(
    async_session_maker, taxonomy
):
    async with async_session_maker() as db:
        store, other = await _parked(db)
        fashion = await db.scalar(select(Category).where(Category.slug == "fashion"))
        electronics = await db.scalar(select(Category).where(Category.slug == "electronics"))
        waiting = await _coupon(db, store, other, source_categories=["Gadget Corner"])
        unrelated = await _coupon(db, store, other, source_categories=["Something Else"])
        placed_elsewhere = await _coupon(db, store, fashion, source_categories=["Gadget Corner"])

        moved = await refile_coupons(db, alias_key="gadget corner", category_id=electronics.id)

        assert moved == 1
        await db.refresh(waiting), await db.refresh(unrelated), await db.refresh(placed_elsewhere)
        assert (waiting.category_id, unrelated.category_id, placed_elsewhere.category_id) == (
            electronics.id, other.id, fashion.id,
        )  # fmt: skip


async def test_recategorizing_moves_what_a_new_category_or_rule_can_now_place(
    async_session_maker, taxonomy
):
    async with async_session_maker() as db:
        store, other = await _parked(db)
        other_id = other.id
        waiting = await _coupon(db, store, other, source_categories=["Kitchen Appliances"])
        stuck = await _coupon(db, store, other, source_categories=["Quantum Widgets"])

        db.add(Category(name="Home & Kitchen", slug="home-kitchen"))
        await db.commit()

        assert await recategorize_fallback(db, CategoryResolver(), dry_run=True) == {
            "home-kitchen": 1
        }
        await db.refresh(waiting)
        assert waiting.category_id == other_id  # a dry run changes nothing

        assert await recategorize_fallback(db, CategoryResolver()) == {"home-kitchen": 1}
        await db.refresh(waiting), await db.refresh(stuck)
        assert await _slug(db, waiting.category_id) == "home-kitchen"
        assert stuck.category_id == other_id


# --- the provider adapter ----------------------------------------------------------


async def test_admitad_category_wording_is_read_from_the_coupon_and_its_programme():
    names = _category_names(
        {
            "categories": [
                {"id": 1, "name": "Computers"},
                {"id": 2, "name": "Laptops", "parent": {"name": "Electronics"}},
            ]
        },
        {"categories": [{"name": "Consumer Electronics"}, "Gadgets"]},
    )
    assert names == ["Computers", "Laptops", "Electronics", "Consumer Electronics", "Gadgets"]
    assert _category_names({}, {}) == []
    assert _category_names({"categories": "not a list"}, {"categories": [{"name": ""}, 5]}) == []


# --- the whole pipeline -------------------------------------------------------------


async def test_ingestion_keeps_every_offer_whatever_its_category_wording(
    async_session_maker, taxonomy
):
    expires = (datetime.now(UTC) + timedelta(days=7)).isoformat()

    def offer(n: int, **extra):
        return {
            "title": f"Offer number {n} with real savings",
            "store_slug": "gadgetworld",
            "store_name": "GadgetWorld",
            "code": f"SAVE{n}0",
            "discount_text": "10% off",
            "destination_url": f"https://www.gadgetworld.com/deal-{n}",
            "expires_at": expires,
            "external_id": f"o{n}",
            **extra,
        }

    offers = [
        offer(1, categories=["Computers"]),
        offer(2, categories=["Electronics & COMPUTERS ACCESORIES", "Computer Accessories"]),
        offer(3, categories=["Home Appliances", "Consumer Electronics"]),
        offer(4, categories=["Quantum Widgets"]),
        offer(5),  # no category wording at all
    ]
    async with async_session_maker() as db:
        source = Source(
            name="Net",
            slug="net",
            kind=SourceKind.static,
            config={"offers": offers},
            is_enabled=True,
        )
        db.add(source)
        await db.commit()
        await db.refresh(source)
        run = await pipeline.run_ingestion(db, source)

        assert (run.fetched, run.created, run.failed) == (5, 5, 0)  # nothing rejected

        rows = (
            await db.execute(
                select(Coupon.title, Category.slug).join(
                    Category, Category.id == Coupon.category_id
                )
            )
        ).all()
        filed = {int(re.search(r"number (\d)", title).group(1)): slug for title, slug in rows}
        assert filed == {
            1: "electronics",
            2: "electronics",
            3: "electronics",
            4: "other",
            5: "other",
        }, rows

        # The review queue holds exactly what a person has to look at.
        pending = (
            (await db.execute(select(CategoryAlias.label).where(CategoryAlias.method == "pending")))
            .scalars()
            .all()
        )
        assert pending == ["Quantum Widgets"]

        # A second run changes nothing about how those offers are filed, and re-counts nothing as new.
        again = await pipeline.run_ingestion(db, source)
        assert again.failed == 0
