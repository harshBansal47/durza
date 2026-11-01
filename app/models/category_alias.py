"""What a provider's category wording means in our taxonomy.

Providers describe categories in their own words ("Electronics & Computers
Accessories", "Consumer Electronics", "Home Appliances") and our taxonomy was built
before any of them were connected. Matching the two exactly rejects almost
everything, and rejecting an offer over a label is the wrong trade: the offer is
real, only our filing of it is unsettled. So the pipeline files it somewhere
sensible and records the wording here, where a person can correct it once and have
every later offer follow.

One row per normalised wording (`alias`), shared by every provider:

* `manual`  - a person chose the category. Always wins.
* `rule`    - a keyword rule chose it (see `app/ingestion/category_rules.py`). Shown
  here so a wrong guess is visible and can be overridden, not buried in the code.
* `pending` - seen, but nothing matched. The offer was filed under the fallback
  category. This is the review queue: map it and the offers move.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin
from app.models.category import Category


class CategoryAliasMethod(str, enum.Enum):
    manual = "manual"
    rule = "rule"
    pending = "pending"


class CategoryAlias(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "category_aliases"
    __table_args__ = (UniqueConstraint("alias", name="uq_category_aliases_alias"),)

    # The lookup key: lower-cased, punctuation stripped, whitespace collapsed.
    alias: Mapped[str] = mapped_column(String(160), nullable=False)
    # The wording as the provider wrote it, for people.
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    # NULL means unmapped. SET NULL on delete, so removing a category sends its
    # wordings back to the review queue instead of deleting what we learned.
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    seen_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_source: Mapped[str | None] = mapped_column(String(80), nullable=True)

    category: Mapped[Category | None] = relationship()

    def __str__(self) -> str:
        return f"{self.label} -> {self.category or 'unmapped'}"
