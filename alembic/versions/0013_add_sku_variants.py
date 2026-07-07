"""add sku_variants + orders.sku_product_id (SKU-master prototype)

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-06
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("sku_product_id", sa.Integer(), nullable=True))
    op.create_table(
        "sku_variants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ggsel_offer_id", sa.Integer(), nullable=False),
        sa.Column("ggsel_option_id", sa.Integer(), nullable=True),
        sa.Column("ggsel_variant_id", sa.Integer(), nullable=False),
        sa.Column("starpets_product_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("price_rub", sa.Numeric(10, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sku_variants_ggsel_offer_id", "sku_variants", ["ggsel_offer_id"])
    op.create_index("ix_sku_variants_ggsel_variant_id", "sku_variants", ["ggsel_variant_id"])


def downgrade() -> None:
    op.drop_index("ix_sku_variants_ggsel_variant_id", table_name="sku_variants")
    op.drop_index("ix_sku_variants_ggsel_offer_id", table_name="sku_variants")
    op.drop_table("sku_variants")
    op.drop_column("orders", "sku_product_id")
