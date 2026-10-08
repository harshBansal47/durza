"""sponsored ads: label, advertiser, alt text, click count; ad clicks in click_events

Revision ID: d7a2b5c8e913
Revises: 59be7248d8e7
Create Date: 2026-10-04 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d7a2b5c8e913"
down_revision = "59be7248d8e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ads", sa.Column("label", sa.String(length=20), server_default="Sponsored", nullable=False)
    )
    op.add_column("ads", sa.Column("advertiser", sa.String(length=120), nullable=True))
    op.add_column("ads", sa.Column("alt_text", sa.String(length=200), nullable=True))
    op.add_column(
        "ads", sa.Column("clicks_count", sa.Integer(), server_default="0", nullable=False)
    )

    op.add_column("click_events", sa.Column("ad_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_click_events_ad_id_ads", "click_events", "ads", ["ad_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_click_events_ad_id", "click_events", ["ad_id"])


def downgrade() -> None:
    op.drop_index("ix_click_events_ad_id", table_name="click_events")
    op.drop_constraint("fk_click_events_ad_id_ads", "click_events", type_="foreignkey")
    op.drop_column("click_events", "ad_id")
    op.drop_column("ads", "clicks_count")
    op.drop_column("ads", "alt_text")
    op.drop_column("ads", "advertiser")
    op.drop_column("ads", "label")
