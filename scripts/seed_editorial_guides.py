"""Seed the editorial CMS with Durza's original buying guides.

What this seeds
---------------
Three hand-written, text-rich buying guides (see ``scripts/data/editorial_guides.json``)
that live at ``/guides/<slug>`` and feed the homepage's "Buying guides" section. The
manifest is checked in; this script only turns it into ``editorial_contents`` rows.

Idempotency
-----------
Guides are matched by slug. Re-running updates the same rows in place — replacing the
content blocks, structured data, FAQs and entity links — rather than duplicating them,
so the published URLs stay stable. The summary prints how many were created vs updated.

Entity links (the "Explore related savings" chips on each article) are resolved by
slug at seed time. A link whose store or category does not exist in the database is
skipped with a warning instead of failing the whole run, because a guide is still
useful without every chip.

Usage
-----
    DATABASE_URL=postgresql+asyncpg://durza:durza@localhost:54330/durza_seed \
        .venv/bin/python scripts/seed_editorial_guides.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.category import Category
from app.models.editorial import EditorialContent
from app.models.store import Store
from app.schemas.editorial import (
    EditorialCreate,
    EditorialEntityLinkWrite,
    EditorialStatus,
    EditorialType,
    EditorialUpdate,
    FAQWrite,
)
from app.services import editorial_service

DATA_PATH = Path(__file__).resolve().parent / "data" / "editorial_guides.json"


def load_manifest() -> dict:
    with DATA_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


async def resolve_entity_links(db, links: list[dict]) -> list[EditorialEntityLinkWrite]:
    """Turn slug references into real foreign keys, skipping anything missing."""
    resolved: list[EditorialEntityLinkWrite] = []
    for position, link in enumerate(links):
        kind = link.get("kind")
        slug = link.get("slug")
        if kind not in {"store", "category"}:
            print(f"  ! unknown entity link kind {kind!r} for {slug!r} — skipped")
            continue
        model = Store if kind == "store" else Category
        row = await db.scalar(select(model).where(model.slug == slug))
        if row is None:
            print(f"  ! {kind} {slug!r} not found — link skipped")
            continue
        target = {"store_id": row.id} if kind == "store" else {"category_id": row.id}
        resolved.append(
            EditorialEntityLinkWrite(
                **target,
                link_type="related",
                anchor_text=link.get("anchor_text"),
                sort_order=position,
            )
        )
    return resolved


def faqs_from(guide: dict) -> list[FAQWrite]:
    return [
        FAQWrite(question=item["question"], answer=item["answer"], sort_order=index)
        for index, item in enumerate(guide.get("faqs", []))
    ]


async def upsert_guide(db, guide: dict) -> str:
    """Create or update one guide by slug; returns 'created' or 'updated'."""
    slug = guide["slug"]
    links = await resolve_entity_links(db, guide.get("entity_links", []))
    faqs = faqs_from(guide)
    existing = await db.scalar(select(EditorialContent).where(EditorialContent.slug == slug))

    if existing is not None:
        await editorial_service.update_content(
            db,
            existing,
            EditorialUpdate(
                title=guide["title"],
                summary=guide["summary"],
                content_blocks=guide["content_blocks"],
                structured_data=guide.get("structured_data", {}),
                hero_image_url=guide.get("hero_image_url"),
                author_name=guide.get("author_name"),
                market=guide.get("market"),
                seo_title=guide.get("seo_title"),
                meta_description=guide.get("meta_description"),
                is_indexable=True,
                is_featured=guide.get("is_featured", False),
                status=EditorialStatus.published,
                entity_links=links,
                faqs=faqs,
            ),
        )
        return "updated"

    await editorial_service.create_content(
        db,
        EditorialCreate(
            content_type=EditorialType.guide,
            title=guide["title"],
            slug=slug,
            summary=guide["summary"],
            content_blocks=guide["content_blocks"],
            structured_data=guide.get("structured_data", {}),
            hero_image_url=guide.get("hero_image_url"),
            author_name=guide.get("author_name"),
            market=guide.get("market"),
            seo_title=guide.get("seo_title"),
            meta_description=guide.get("meta_description"),
            is_indexable=True,
            is_featured=guide.get("is_featured", False),
            status=EditorialStatus.published,
            entity_links=links,
            faqs=faqs,
        ),
    )
    return "created"


async def run(dry_run: bool) -> None:
    manifest = load_manifest()
    created = updated = 0
    async with AsyncSessionLocal() as db:
        for guide in manifest["guides"]:
            action = await upsert_guide(db, guide)
            if action == "created":
                created += 1
            else:
                updated += 1
            print(f"  {action}: /guides/{guide['slug']}")
        if dry_run:
            await db.rollback()
            print("\nDry run — nothing written.")
        else:
            await db.commit()
            print(f"\nSeeded {created} new and updated {updated} guides.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print without writing")
    args = parser.parse_args()
    asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    main()
