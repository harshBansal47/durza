import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class Category(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "categories"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True, nullable=False)
    icon: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Self-referencing FK: a category with a parent_id is a subcategory.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    # Many-to-one to itself. Used by the admin dropdown; the JSON API ignores it.
    parent: Mapped["Category | None"] = relationship(remote_side="Category.id")

    def __str__(self) -> str:
        return self.name
