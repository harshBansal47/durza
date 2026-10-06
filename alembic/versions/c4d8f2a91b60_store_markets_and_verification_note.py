"""store market columns + optional verification note

Savevero lists stores per country, so a store needs to know which market it
belongs to and which currency its prices are in. Verifications get an optional
note so a conditional code can explain itself ("works, but excludes sale items").

Revision ID: c4d8f2a91b60
Revises: 2381a1c204f9
Create Date: 2026-10-02 23:10:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c4d8f2a91b60"
down_revision = "2381a1c204f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stores") as batch:
        batch.add_column(sa.Column("country_code", sa.String(length=2), nullable=True))
        batch.add_column(sa.Column("currency", sa.String(length=3), nullable=True))
        # Index for the per-country listing filter.
        batch.create_index("ix_stores_country_code", ["country_code"])

    with op.batch_alter_table("coupon_verifications") as batch:
        batch.add_column(sa.Column("note", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("coupon_verifications") as batch:
        batch.drop_column("note")

    with op.batch_alter_table("stores") as batch:
        batch.drop_index("ix_stores_country_code")
        batch.drop_column("currency")
        batch.drop_column("country_code")
