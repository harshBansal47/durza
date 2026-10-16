"""Attribution service.

Manages the creation and lookup of attribution records. When a conversion
comes in from an affiliate network, this service:
1. Finds the matching click by clickref
2. Creates an attribution record linking the click to the conversion
3. Updates first-touch and last-touch timestamps

The attribution model used is always `click_touch` for affiliate
conversions, because that is how affiliate networks credit conversions.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attribution import Attribution, AttributionModel
from app.models.click import ClickEvent
from app.models.conversion import Conversion


async def find_attribution_by_click(
    db: AsyncSession,
    click_id: str,
) -> Attribution | None:
    """Find an existing attribution record by click ID."""
    result = await db.execute(select(Attribution).where(Attribution.click_id == click_id))
    return result.scalar_one_or_none()


async def create_attribution(
    db: AsyncSession,
    *,
    click: ClickEvent,
    visitor_id: str,
    session_id: str | None = None,
    attribution_model: AttributionModel = AttributionModel.click_touch,
    attribution_window: int = 30,
    reason: str = "matched by clickref",
) -> Attribution:
    """Create an attribution record for a click.

    This is called when a click is recorded, to establish the link
    between the visitor and the click. When a conversion later comes
    in with the same clickref, the attribution record is updated with
    the conversion timestamp.
    """
    now = datetime.now(UTC)

    attribution = Attribution(
        visitor_id=visitor_id,
        session_id=session_id,
        coupon_id=click.coupon_id,
        merchant_id=click.store_id,
        click_id=click.clickref,
        click_event_id=click.id,
        attribution_model=attribution_model,
        first_touch_at=now,
        last_touch_at=now,
        attribution_window=attribution_window,
        attribution_reason=reason,
    )
    db.add(attribution)
    await db.commit()
    await db.refresh(attribution)
    return attribution


async def record_conversion_attribution(
    db: AsyncSession,
    conversion: Conversion,
) -> Attribution | None:
    """Record attribution for a conversion reported by the network.

    This is called when an affiliate network reports a conversion.
    It finds the matching click by clickref and creates or updates
    the attribution record.

    Returns the attribution record, or None if no matching click was found.
    """
    if not conversion.clickref:
        return None

    # Find the matching click
    result = await db.execute(select(ClickEvent).where(ClickEvent.clickref == conversion.clickref))
    click = result.scalar_one_or_none()
    if not click:
        return None

    # Check if attribution already exists for this click
    existing = await find_attribution_by_click(db, conversion.clickref)
    if existing:
        # Update the existing attribution with conversion details
        existing.converted_at = conversion.transaction_at or datetime.now(UTC)
        existing.last_touch_at = click.created_at
        await db.commit()
        await db.refresh(existing)
        return existing

    # Create new attribution
    now = datetime.now(UTC)
    attribution = Attribution(
        visitor_id=click.visitor_hash or "unknown",
        session_id=None,  # Would be populated from session tracking
        coupon_id=click.coupon_id,
        merchant_id=click.store_id,
        click_id=click.clickref,
        click_event_id=click.id,
        attribution_model=AttributionModel.click_touch,
        first_touch_at=click.created_at,
        last_touch_at=click.created_at,
        converted_at=conversion.transaction_at or now,
        attribution_window=30,  # Default, would be from program
        attribution_reason="matched by clickref from network conversion report",
    )
    db.add(attribution)
    await db.commit()
    await db.refresh(attribution)
    return attribution


async def get_attribution_window_days(
    db: AsyncSession,
    program_id: str,
) -> int:
    """Get the attribution window (cookie duration) for a program.

    Defaults to 30 days if the program is not found.
    """
    from uuid import UUID as UUIDType

    from app.models.affiliate_program import AffiliateProgram

    try:
        program_uuid = UUIDType(program_id) if isinstance(program_id, str) else program_id
    except (ValueError, TypeError):
        return 30

    program = await db.get(AffiliateProgram, program_uuid)
    if not program:
        return 30
    return program.cookie_duration_days


def is_within_attribution_window(
    attribution: Attribution,
    conversion_at: datetime,
) -> bool:
    """Check if a conversion is within the attribution window.

    The conversion must happen within `attribution_window` days of the
    last touch (click) to be attributed.
    """
    if not attribution.last_touch_at:
        return False

    # Handle naive datetimes from SQLite
    last_touch = attribution.last_touch_at
    if last_touch.tzinfo is None:
        last_touch = last_touch.replace(tzinfo=UTC)

    # Also handle naive conversion_at
    conv_at = conversion_at
    if conv_at.tzinfo is None:
        conv_at = conv_at.replace(tzinfo=UTC)

    window = timedelta(days=attribution.attribution_window)
    return conv_at <= last_touch + window
