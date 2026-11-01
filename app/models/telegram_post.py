"""The ledger behind "each offer reaches the Telegram channel once".

One row per offer the poster has considered, written *before* the message is
sent. That ordering is the whole point: the unique constraints turn "did we
already post this?" into something the database enforces, so a restart, a second
worker or a crash between the send and the bookkeeping can never produce a
duplicate post. The price is that a crash in that one gap loses a post instead of
doubling it, which for a public channel is the right way round.

Status:

* `pending`   - claimed, send in flight. A row that stays here is a crash in the
  gap above; it is never re-sent, and shows up in the admin.
* `sent`      - Telegram accepted it. `message_id` is set.
* `rejected`  - Telegram refused this particular message (bad markup, bad URL).
  Retrying the same text cannot succeed, so it is not retried.
* `uncertain` - the request may or may not have been delivered (a read timeout).
  Re-sending could duplicate, so it is not re-sent.
* `duplicate` - the same offer had already been posted under another coupon row.
  Recorded so the row stops being picked up as "new" on every scan.

A send that definitely did not happen (cannot connect, rate limited, bot not
allowed to post) deletes its claim instead, so the offer is simply tried again.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class TelegramPostStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    rejected = "rejected"
    uncertain = "uncertain"
    duplicate = "duplicate"


class TelegramPost(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "telegram_posts"
    __table_args__ = (
        UniqueConstraint("coupon_id", name="uq_telegram_posts_coupon_id"),
        UniqueConstraint("dedupe_key", name="uq_telegram_posts_dedupe_key"),
    )

    # SET NULL, not CASCADE: deleting a coupon must not delete the record that it
    # was posted, or the same offer could be posted again when it is re-imported.
    coupon_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("coupons.id", ondelete="SET NULL"), nullable=True
    )
    # Hash of what makes an offer *the same offer* (store, code, discount), so a
    # copy of it arriving as a new coupon row is recognised. See telegram_service.
    dedupe_key: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Snapshots, so the admin list still reads sensibly after the coupon is gone.
    store_name: Mapped[str] = mapped_column(String(150), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __str__(self) -> str:
        return f"{self.status}: {self.store_name} - {self.title[:50]}"
