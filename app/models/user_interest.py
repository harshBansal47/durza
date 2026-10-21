import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Float, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class UserInterest(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "user_interests"

    __table_args__ = (
        UniqueConstraint("user_id", "interest_type", "interest_key", name="uq_user_interest"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    interest_type: Mapped[str] = mapped_column(String(20), nullable=False)
    interest_key: Mapped[str] = mapped_column(String(120), nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
