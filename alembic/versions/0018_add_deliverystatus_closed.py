"""add 'closed' value to deliverystatus enum (manual terminal 'handled' status)

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-10
"""
from typing import Sequence, Union
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE can't run inside a transaction block -> autocommit
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE deliverystatus ADD VALUE IF NOT EXISTS 'closed'")


def downgrade() -> None:
    # Postgres has no clean DROP VALUE; leave the enum value in place.
    pass
