import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class AdPosition(str, enum.Enum):
    header = "header"
    sidebar = "sidebar"
    footer = "footer"


# What a placement may call itself. Never blank, never free text: the label is
# the shopper's only signal that this is paid, so it is constrained here and in
# the schema rather than left to whoever fills in the admin form.
AD_LABELS = ("Sponsored", "Partner", "Ad")


class Ad(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "ads"

    position: Mapped[AdPosition] = mapped_column(
        Enum(AdPosition, name="ad_position", native_enum=True), nullable=False
    )
    image_url: Mapped[str] = mapped_column(String(500), nullable=False)
    target_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Disclosure + presentation. `target_url` is never returned by the public API;
    # clicks go through GET /ads/{id}/go so each one is recorded.
    label: Mapped[str] = mapped_column(
        String(20), default="Sponsored", server_default="Sponsored", nullable=False
    )
    advertiser: Mapped[str | None] = mapped_column(String(120), nullable=True)
    alt_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    clicks_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
