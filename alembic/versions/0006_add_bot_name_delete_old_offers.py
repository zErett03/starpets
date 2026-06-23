"""add bot_name to orders, delete stale offers

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-23
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STALE_OFFER_IDS = [102217598, 102217599, 102217600, 102217601, 102217602]


def upgrade() -> None:
    op.add_column("orders", sa.Column("bot_name", sa.String(), nullable=True))
    op.execute(
        f"UPDATE offers SET ggsel_offer_id = NULL WHERE ggsel_offer_id IN ({','.join(str(i) for i in _STALE_OFFER_IDS)})"
    )


def downgrade() -> None:
    op.drop_column("orders", "bot_name")
