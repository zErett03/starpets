"""add store_items for event-driven price sync

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-05
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "store_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("price_usd", sa.Numeric(10, 3), nullable=True),
        sa.Column("reserve_level", sa.SmallInteger(), server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_store_items_product_id", "store_items", ["product_id"])


def downgrade() -> None:
    op.drop_index("ix_store_items_product_id", table_name="store_items")
    op.drop_table("store_items")
