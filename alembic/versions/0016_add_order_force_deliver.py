"""add orders.force_deliver (operator override to buy even at a loss)

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-08
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("force_deliver", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("orders", "force_deliver")
