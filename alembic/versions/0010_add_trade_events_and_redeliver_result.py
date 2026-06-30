"""add trade_events table + orders.last_redeliver_result

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-01
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("last_redeliver_result", sa.Text(), nullable=True),
    )
    op.create_table(
        "trade_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("trade_id", sa.String(), nullable=True),
        sa.Column("sp_event_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.SmallInteger(), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=True),
        sa.Column("bot_name", sa.String(), nullable=True),
        sa.Column("data_json", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("trade_id", "sp_event_id", name="uq_trade_events_trade_event"),
    )
    op.create_index("ix_trade_events_order_id", "trade_events", ["order_id"])
    op.create_index("ix_trade_events_trade_id", "trade_events", ["trade_id"])


def downgrade() -> None:
    op.drop_index("ix_trade_events_trade_id", table_name="trade_events")
    op.drop_index("ix_trade_events_order_id", table_name="trade_events")
    op.drop_table("trade_events")
    op.drop_column("orders", "last_redeliver_result")
