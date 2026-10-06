"""Non-impression product events: the shopper's interactions, as the storefront reports them.

Impressions have their own table and endpoint (`coupon_impression`) because ranking
reads them at volume. Everything else a shopper does on the site (reveals a code, copies
it, saves a deal, searches) lands here, so conversion funnels such as reveal -> copy ->
click-through can be measured from one place instead of a second analytics system.

Deliberately has no foreign keys: an event about a coupon that was later deleted must
still count, and a client sending a stale id must not be able to make an insert fail.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UUIDPKMixin


class AnalyticsEvent(Base, UUIDPKMixin):
    __tablename__ = "analytics_events"
    __table_args__ = (Index("ix_analytics_events_name_created", "name", "created_at"),)

    name: Mapped[str] = mapped_column(String(40), nullable=False)
    visitor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    coupon_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, nullable=True
    )
    store_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    page_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Small, sanitized, scalar-only properties (see sanitize_props in the router).
    props: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
