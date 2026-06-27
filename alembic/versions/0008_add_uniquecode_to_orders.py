"""add uniquecode to orders

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-27
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("uniquecode", sa.String(), nullable=True),
    )
    op.create_index("ix_orders_uniquecode", "orders", ["uniquecode"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_orders_uniquecode", table_name="orders")
    op.drop_column("orders", "uniquecode")
