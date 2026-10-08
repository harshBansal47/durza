import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.category import Category
    from app.models.store import Store


class Product(Base, UUIDPKMixin, TimestampMixin):
    """Something a store sells. Denormalized price stats are maintained by
    price_service.record_price every time a PricePoint lands."""

    __tablename__ = "products"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(220), unique=True, index=True, nullable=False)
    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True, nullable=False
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    current_price: Mapped[float | None] = mapped_column(
        Numeric(10, 2, asdecimal=False), nullable=True
    )
    list_price: Mapped[float | None] = mapped_column(Numeric(10, 2, asdecimal=False), nullable=True)
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lowest_price_7d: Mapped[float | None] = mapped_column(
        Numeric(10, 2, asdecimal=False), nullable=True
    )
    lowest_price_30d: Mapped[float | None] = mapped_column(
        Numeric(10, 2, asdecimal=False), nullable=True
    )
    lowest_price_90d: Mapped[float | None] = mapped_column(
        Numeric(10, 2, asdecimal=False), nullable=True
    )
    # Price-drop signal: populated when a new point is cheaper than the previous one.
    last_price_drop_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_price_drop_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)

    store: Mapped["Store"] = relationship()
    category: Mapped["Category"] = relationship()

    @property
    def has_url(self) -> bool:
        """Whether a buy link exists. The link itself is only reachable via
        GET /products/{id}/go, never in public JSON."""
        return bool(self.url)

    def __str__(self) -> str:
        return self.name


class PricePoint(Base, UUIDPKMixin, TimestampMixin):
    """One observed price for a product at a moment in time — the history."""

    __tablename__ = "price_points"

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True, nullable=False
    )
    price: Mapped[float] = mapped_column(Numeric(10, 2, asdecimal=False), nullable=False)
    original_price: Mapped[float | None] = mapped_column(
        Numeric(10, 2, asdecimal=False), nullable=True
    )
    shipping: Mapped[float] = mapped_column(
        Numeric(10, 2, asdecimal=False), default=0, nullable=False
    )
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # The coupon that applied to this price, if any — drives effective price.
    coupon_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("coupons.id", ondelete="SET NULL"), nullable=True
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    def __str__(self) -> str:
        return f"{self.product_id} @ {self.price}"
