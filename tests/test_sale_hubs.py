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
