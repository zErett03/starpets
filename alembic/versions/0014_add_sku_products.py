"""add sku_products (SKU-master product catalog with pumping)

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-07
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sku_products",
        sa.Column("product_id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("rare", sa.String(), nullable=True),
        sa.Column("item_type", sa.String(), nullable=True),
        sa.Column("age", sa.String(), nullable=True),
        sa.Column("pumping", sa.String(), nullable=True),
        sa.Column("flyable", sa.Boolean(), nullable=True),
        sa.Column("rideable", sa.Boolean(), nullable=True),
        sa.Column("image_uri", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sku_products_name", "sku_products", ["name"])


def downgrade() -> None:
    op.drop_index("ix_sku_products_name", table_name="sku_products")
    op.drop_table("sku_products")
