"""composite unique constraint on offers (name+item_type+rare+flyable+rideable+age)

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-16

Requires PostgreSQL 15+ for NULLS NOT DISTINCT.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("ALTER TABLE offers DROP CONSTRAINT IF EXISTS offers_name_key"))
    conn.execute(sa.text("""
        ALTER TABLE offers
        ADD CONSTRAINT uq_offers_composite
        UNIQUE NULLS NOT DISTINCT (name, item_type, rare, flyable, rideable, age)
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("ALTER TABLE offers DROP CONSTRAINT IF EXISTS uq_offers_composite"))
    conn.execute(sa.text("ALTER TABLE offers ADD CONSTRAINT offers_name_key UNIQUE (name)"))
