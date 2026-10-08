import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class ConversionStatus(str, enum.Enum):
    """Lifecycle of an affiliate transaction as reported by the network."""

    pending = "pending"
    approved = "approved"
    declined = "declined"
    deleted = "deleted"


class Conversion(Base, UUIDPKMixin, TimestampMixin):
    """One affiliate transaction (conversion) as reported by the network.

    A conversion links back to a `ClickEvent` via `clickref`. The same network
    transaction id will never be inserted twice thanks to the unique constraint
    on (network, network_txn_id). A conversion can exist without a matching
    click (`click_id` NULL) — e.g. the click happened before tracking was added
    or the clickref was corrupted. Such rows are counted as "unmatched" but are
    kept for auditability.

    Status transitions are: pending -> approved | declined | deleted.
    A sale that was pending can later become approved/declined/deleted.
    """

    __tablename__ = "conversions"

    __table_args__ = (
        # One row per network transaction
        Index("ix_conversions_network_txn", "network", "network_txn_id", unique=True),
        # Lookups by store and time window
        Index("ix_conversions_store_txn", "store_id", "transaction_at"),
    )

    # Network identity (Awin, etc.) — never null, source of truth for deduplication
    network: Mapped[str] = mapped_column(String(20), nullable=False)
    network_txn_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Optional link back to the click that originated this conversion
    clickref: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    # The click that this conversion is attributed to (may be NULL if no match)
    click_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("click_events.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # Store for reporting/grouping; SET NULL so deleting a store doesn't erase revenue history
    store_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("stores.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Lifecycle state of this transaction as reported by the network
    status: Mapped[ConversionStatus] = mapped_column(
        Enum(ConversionStatus, name="conversion_status", native_enum=True), nullable=False
    )

    # Monetary amounts: always Decimal, grouped by currency (never mixed)
    sale_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    commission_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    # When the sale/transaction happened on the network
    transaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the click that led to this transaction happened
    click_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the network validated/declined the transaction
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # If declined/deleted, why?
    decline_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    def __str__(self) -> str:
        return f"{self.network}:{self.network_txn_id} ({self.status})"
