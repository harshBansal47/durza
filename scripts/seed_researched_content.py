"""Seed the catalogue with hand-researched content from official sources.

What this seeds
---------------
Categories, stores and offers researched by hand on 2026-10-17 from official
sources only - store offer pages, store-published T&C pages and bank offer
pages. The full provenance record (source URL, check date, paraphrased terms
per item) lives in ``scripts/data/researched_offers.json``, which is checked
in; this script only turns that manifest into rows.

Honesty notes
-------------
* Every offer ingests as ``unverified`` - the normal state for feed data. No
  code has been tested at checkout, and none claims to have been.
* Coupon codes exist only where an official page published them; nothing was
  taken from other coupon sites.
* Stores that yielded no verifiable offer on the research date are still
  created (with a description); the research skips are printed at the end
  with their reasons.

Idempotency
-----------
Offers are matched by (store, code) or, for code-less deals, by
(source, external_id) - so re-running never duplicates a coupon. Editing the
manifest and re-running updates the same rows and refreshes the source
config. The summary table shows "New" (created this run) separately from the
total so a second run visibly creates nothing.

Usage
-----
    ENVIRONMENT=test DATABASE_URL=postgresql+asyncpg://durza:durza@localhost:54330/durza_seed \
        .venv/bin/python scripts/seed_researched_content.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.core.database import AsyncSessionLocal
from app.ingestion import merchants
from app.ingestion.pipeline import run_ingestion
from app.models.category import Category
from app.models.coupon import Coupon
from app.models.source import Source, SourceKind
from app.models.store import Store, StoreStatus

DATA_PATH = Path(__file__).resolve().parent / "data" / "researched_offers.json"
SOURCE_SLUG = "researched-official-pages"
SOURCE_NAME = "Researched official pages"

# Soft bounds for the store descriptions (target 80-120 words). Out-of-range
# values are printed as warnings rather than failing the run: the description
# is editorial work, not pipeline data.
DESCRIPTION_WORDS_MIN = 70
DESCRIPTION_WORDS_MAX = 130


def load_manifest() -> dict[str, Any]:
    with DATA_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def validate_manifest(data: dict[str, Any]) -> list[str]:
    """Hard errors raise; soft issues come back as warnings to print."""
    warnings: list[str] = []

    category_slugs = {c["slug"] for c in data["categories"]}
    store_slugs = {s["slug"] for s in data["stores"]}
    store_domains = {s["domain"] for s in data["stores"]}

    if len(category_slugs) != len(data["categories"]):
        raise ValueError("duplicate category slugs in manifest")
    if len(store_slugs) != len(data["stores"]) or len(store_domains) != len(data["stores"]):
        raise ValueError("duplicate store slug or domain in manifest")

    seen_external_ids: set[str] = set()
    seen_codes: set[tuple[str, str]] = set()
    per_store_items: dict[str, int] = {slug: 0 for slug in store_slugs}

    for offer in data["offers"]:
        external_id = offer["external_id"]
        if external_id in seen_external_ids:
            raise ValueError(f"duplicate external_id: {external_id}")
        seen_external_ids.add(external_id)

        if offer["store_slug"] not in store_slugs:
            raise ValueError(f"{external_id}: unknown store_slug {offer['store_slug']!r}")
        if offer["category_slug"] not in category_slugs:
            raise ValueError(f"{external_id}: unknown category_slug {offer['category_slug']!r}")
        if offer["discount_type"] not in {"percentage", "fixed", "deal"}:
            raise ValueError(f"{external_id}: bad discount_type {offer['discount_type']!r}")
        if not offer["destination_url"].startswith("https://"):
            raise ValueError(f"{external_id}: destination_url must be https")
        if not offer["source_url"].startswith("https://"):
            raise ValueError(f"{external_id}: source_url must be https")
        if len(offer["title"]) > 200:
            raise ValueError(f"{external_id}: title exceeds 200 chars")
        if not offer.get("terms"):
            raise ValueError(f"{external_id}: missing terms (provenance requirement)")

        code = offer.get("code")
        if code:
            key = (offer["store_slug"], code.upper())
            if key in seen_codes:
                # Two offers sharing (store, code) collapse into one coupon in
                # the pipeline on purpose - keeping them separate in the
                # manifest would just make the summary lie about the counts.
                warnings.append(
                    f"{external_id}: code {code} repeats for store "
                    f"{offer['store_slug']} - the pipeline will merge them"
                )
            seen_codes.add(key)

        per_store_items[offer["store_slug"]] += 1

    for store in data["stores"]:
        normalized = merchants.normalize_domain(store["website_url"])
        if normalized != store["domain"]:
            raise ValueError(
                f"store {store['slug']}: website_url normalizes to {normalized!r}, "
                f"manifest says {store['domain']!r}"
            )
        description = store.get("description")
        if description:
            words = len(description.split())
            if not DESCRIPTION_WORDS_MIN <= words <= DESCRIPTION_WORDS_MAX:
                warnings.append(
                    f"store {store['slug']}: description is {words} words "
                    f"(target {DESCRIPTION_WORDS_MIN}-{DESCRIPTION_WORDS_MAX})"
                )

    for store in data["stores"]:
        if per_store_items.get(store["slug"], 0) == 0:
            pass  # zero-item stores are expected and reported in the summary

    if len(data["offers"]) != len(seen_external_ids):
        raise ValueError("duplicate offers detected")
    return warnings


def build_source_offers(data: dict[str, Any], stores: dict[str, Store]) -> list[dict[str, Any]]:
    """Turn manifest offers into RawOffer-shaped dicts for the static source."""
    offers_config: list[dict[str, Any]] = []
    for offer in data["offers"]:
        store = stores[offer["store_slug"]]
        raw: dict[str, Any] = {
            "external_id": offer["external_id"],
            "title": offer["title"],
            "store_slug": store.slug,
            "store_name": store.name,
            "store_website": store.website_url,
            "category_slug": offer["category_slug"],
            "destination_url": offer["destination_url"],
        }
        for key in ("code", "description", "discount_type", "discount_value", "expires_at"):
            if offer.get(key) is not None:
                raw[key] = offer[key]
        extra: dict[str, Any] = {}
        if offer.get("terms"):
            extra["terms"] = offer["terms"]
        if offer.get("minimum_order"):
            extra["minimum_order"] = offer["minimum_order"]
        if extra:
            raw["extra"] = extra
        offers_config.append(raw)
    return offers_config


async def ensure_categories(db, data: dict[str, Any]) -> tuple[dict[str, Category], int]:
    categories: dict[str, Category] = {}
    added = 0
    for entry in data["categories"]:
        existing = (
            await db.execute(select(Category).where(Category.slug == entry["slug"]))
        ).scalar_one_or_none()
        if existing is not None:
            categories[entry["slug"]] = existing
            continue
        category = Category(name=entry["name"], slug=entry["slug"])
        db.add(category)
        categories[entry["slug"]] = category
        added += 1
    await db.flush()
    return categories, added


async def ensure_stores(db, data: dict[str, Any]) -> tuple[dict[str, Store], int]:
    stores: dict[str, Store] = {}
    added = 0
    for entry in data["stores"]:
        domain = merchants.normalize_domain(entry["website_url"])
        existing = (
            await db.execute(select(Store).where(Store.normalized_domain == domain))
        ).scalar_one_or_none()
        if existing is not None:
            stores[entry["slug"]] = existing
            continue
        store = Store(
            name=entry["name"],
            slug=entry["slug"],
            normalized_name=merchants.normalize_name(entry["name"]) or entry["slug"],
            normalized_domain=domain,
            website_url=entry["website_url"],
            description=entry.get("description"),
            aliases=[],
            status=StoreStatus.active,
            country_code="IN",
            currency="INR",
            is_active=True,
        )
        db.add(store)
        stores[entry["slug"]] = store
        added += 1
    await db.flush()
    return stores, added


async def ensure_source(db, offers_config: list[dict[str, Any]]) -> tuple[Source, bool]:
    source = (
        await db.execute(select(Source).where(Source.slug == SOURCE_SLUG))
    ).scalar_one_or_none()
    created = False
    if source is None:
        source = Source(
            name=SOURCE_NAME,
            slug=SOURCE_SLUG,
            kind=SourceKind.static,
            config={"offers": offers_config},
            is_enabled=True,
            probe_destinations=False,
            description=(
                "Hand-researched offers verified from official store pages, bank "
                "offer pages and published T&Cs on 2026-10-17. Provenance for every "
                "item lives in scripts/data/researched_offers.json. All offers are "
                "unverified - no code has been tested at checkout."
            ),
            priority=20,
            confidence=0.6,
            countries=["IN"],
            sync_interval_minutes=1440,
        )
        db.add(source)
        created = True
    else:
        # Rebuilt from the manifest on every run so edits to the data file are
        # picked up; assigned as a new dict so SQLAlchemy sees the change.
        source.config = {"offers": offers_config}
    await db.flush()
    return source, created


async def counts_by_store(db) -> dict[Any, int]:
    rows = await db.execute(
        select(Coupon.store_id, func.count(Coupon.id)).group_by(Coupon.store_id)
    )
    return {store_id: count for store_id, count in rows.all()}


async def main(dry_run: bool) -> None:
    data = load_manifest()
    warnings = validate_manifest(data)

    print(f"Manifest: {DATA_PATH}")
    print(
        f"  {len(data['stores'])} stores, {len(data['offers'])} offers, "
        f"{len(data['skipped'])} skipped research items"
    )
    for warning in warnings:
        print(f"  WARNING: {warning}")
    if dry_run:
        per_store: dict[str, int] = {}
        for offer in data["offers"]:
            per_store[offer["store_slug"]] = per_store.get(offer["store_slug"], 0) + 1
        print("\nOffers per store (dry run, no database writes):")
        for store in data["stores"]:
            print(f"  {store['name']:<20} {per_store.get(store['slug'], 0):>3}")
        return

    async with AsyncSessionLocal() as db:
        categories, categories_added = await ensure_categories(db, data)
        stores, stores_added = await ensure_stores(db, data)
        offers_config = build_source_offers(data, stores)
        source, source_created = await ensure_source(db, offers_config)
        before = await counts_by_store(db)
        await db.commit()

        run = await run_ingestion(db, source, triggered_by="seed_researched_content")
        await db.commit()

        after = await counts_by_store(db)

    print("\nSeed summary")
    print("=" * 62)
    print(
        f"Categories: {len(categories)} total "
        f"({categories_added} added, {len(categories) - categories_added} existing)"
    )
    print(
        f"Stores:     {len(stores)} total "
        f"({stores_added} added, {len(stores) - stores_added} existing)"
    )
    print(f"Source:     {source.slug} ({'created' if source_created else 'config refreshed'})")
    print(
        f"Ingestion:  status={run.status.value} fetched={run.fetched} "
        f"created={run.created} updated={run.updated} failed={run.failed} "
        f"expired={run.expired}"
    )
    if run.error:
        print(f"Ingestion error: {run.error}")
    if run.failed:
        print("WARNING: some offers failed to ingest - see the run row / logs.")

    manifest_count: dict[str, int] = {store["slug"]: 0 for store in data["stores"]}
    category_of: dict[str, str] = {}
    for store in data["stores"]:
        category_of[store["slug"]] = store["category_slug"]
    for offer in data["offers"]:
        manifest_count[offer["store_slug"]] += 1

    print("\nPer store")
    print(f"  {'Store':<18}{'Category':<14}{'In manifest':>11}{'New':>5}{'Total now':>10}")
    for store_entry in data["stores"]:
        store = stores[store_entry["slug"]]
        total = after.get(store.id, 0)
        new = total - before.get(store.id, 0)
        print(
            f"  {store_entry['name']:<18}{category_of[store_entry['slug']]:<14}"
            f"{manifest_count[store_entry['slug']]:>11}{new:>5}{total:>10}"
        )

    print("\nSkipped during research (not seeded, with reasons)")
    print("-" * 62)
    for item in data["skipped"]:
        print(f"  {item['store']}: {item['reason']}")
        print(f"    item: {item['item']}  [{item['source_url']}]")

    print(
        "\nAll offers were ingested as unverified (no code tested at checkout). "
        "Provenance for every item: scripts/data/researched_offers.json"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the manifest and print planned counts without touching the database.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
