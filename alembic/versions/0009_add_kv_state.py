"""add kv_state table

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-30
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kv_state",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("kv_state")
