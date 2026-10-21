import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.slugs import generate_unique_slug
from app.models.editorial import EditorialContent
from app.models.sale import SaleEdition, SaleEditionCouponRule, SaleSeries

CONTENT_REF_RE = re.compile(r"^sale:[a-z0-9:_-]{1,80}$")
MAX_RULES = 20


def content_ref(series_slug: str, edition_slug: str) -> str:
    return f"sale:{series_slug}:{edition_slug}"


def validate_content_ref(value: str) -> bool:
    return bool(CONTENT_REF_RE.match(value)) and len(value) <= 80


def validate_rule(rule_type: str, rule_value: str) -> None:
    if rule_type not in {"store", "tag", "category"}:
        raise ValueError("invalid rule type")
    if not rule_value.strip():
        raise ValueError("rule value required")


async def create_series(db: AsyncSession, *, slug: str, title: str, description: str = "") -> SaleSeries:
    if len(slug) > 40:
        raise ValueError("series slug too long")
    series = SaleSeries(slug=slug, title=title, description=description, publication_status="draft")
    db.add(series)
    await db.commit()
    await db.refresh(series)
    return series


async def create_edition(
    db: AsyncSession,
    *,
    series: SaleSeries,
    slug: str,
    year: int,
    start_at=None,
    end_at=None,
    dates_confirmed: bool = False,
    source_url: str | None = None,
    editorial_content_id=None,
) -> SaleEdition:
    if len(slug) > 30:
        raise ValueError("edition slug too long")
    if dates_confirmed and not source_url:
        raise ValueError("confirmed date requires source_url")
    edition = SaleEdition(
        series_id=series.id,
        slug=slug,
        year=year,
        start_at=start_at,
        end_at=end_at,
        dates_confirmed=dates_confirmed,
        source_url=source_url,
        editorial_content_id=editorial_content_id,
        publication_status="draft",
        lifecycle_status="announced",
    )
    db.add(edition)
    await db.commit()
    await db.refresh(edition)
    return edition


async def _editorial_published(db: AsyncSession, content_id) -> bool:
    if content_id is None:
        return False
    content = await db.get(EditorialContent, content_id)
    return bool(content and content.status == "published")


async def publish_edition(db: AsyncSession, edition: SaleEdition) -> SaleEdition:
    if edition.dates_confirmed and not edition.source_url:
        raise ValueError("confirmed date requires source_url")
    if not await _editorial_published(db, edition.editorial_content_id):
        raise ValueError("linked editorial content must be published")
    edition.publication_status = "published"
    await db.commit()
    await db.refresh(edition)
    return edition


async def archive_edition(db: AsyncSession, edition: SaleEdition, *, reason: str, by: str) -> SaleEdition:
    edition.lifecycle_status = "archived"
    edition.manual_override = True
    edition.manual_override_reason = reason
    edition.manual_override_by = by
    edition.manual_override_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(edition)
    return edition


def transition_lifecycle(edition: SaleEdition, now: datetime) -> str:
    if edition.manual_override:
        return edition.lifecycle_status
    if not edition.dates_confirmed or edition.start_at is None:
        return "announced"
    start_at = edition.start_at
    if start_at is not None and start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=UTC)
    if start_at is not None and now < start_at:
        return "upcoming"
    end_at = edition.end_at
    if end_at is not None and end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=UTC)
    if end_at is None or now <= end_at:
        return "live"
    if edition.archived_after_days and edition.end_at and now > edition.end_at + timedelta(days=edition.archived_after_days):
        return "archived"
    return "ended"


async def run_lifecycle_job(db: AsyncSession, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    editions = (await db.execute(select(SaleEdition))).scalars().all()
    changed = 0
    for edition in editions:
        new_status = transition_lifecycle(edition, now)
        if new_status != edition.lifecycle_status:
            edition.lifecycle_status = new_status
            changed += 1
    await db.commit()
    return changed


async def current_edition(db: AsyncSession, series: SaleSeries) -> SaleEdition | None:
    editions = (await db.execute(select(SaleEdition).where(SaleEdition.series_id == series.id))).scalars().all()
    live = [e for e in editions if e.lifecycle_status == "live" and e.publication_status == "published"]
    if live:
        return max(live, key=lambda e: e.start_at or datetime.min.replace(tzinfo=UTC))
    upcoming = [e for e in editions if e.lifecycle_status == "upcoming" and e.publication_status == "published"]
    if upcoming:
        return min(upcoming, key=lambda e: e.start_at or datetime.max.replace(tzinfo=UTC))
    announced = [e for e in editions if e.lifecycle_status == "announced" and e.publication_status == "published"]
    if announced:
        return announced[0]
    ended = [e for e in editions if e.lifecycle_status == "ended" and e.publication_status == "published"]
    if ended:
        return max(ended, key=lambda e: e.end_at or datetime.min.replace(tzinfo=UTC))
    return None


async def add_rule(db: AsyncSession, edition: SaleEdition, rule_type: str, rule_value: str) -> SaleEditionCouponRule:
    validate_rule(rule_type, rule_value)
    count = len(edition.rules)
    if count >= MAX_RULES:
        raise ValueError("too many rules")
    rule = SaleEditionCouponRule(edition_id=edition.id, rule_type=rule_type, rule_value=rule_value, version=edition.coupon_rules_version)
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule
