"""order_problems — учёт проблемных случаев по заказам

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-10

Проблемы с выдачей до сих пор жили в голове оператора и в переписке: какой бот не принимает
в друзья, сколько раз пересоздавали трейд, по каким предметам это повторяется. Ответить на
вопрос «этот бот проблемный или так совпало» было нечем. Теперь каждый случай пишется
строкой, со снимком бота, трейда и счётчиков на момент пометки.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "order_problems",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("bot_name", sa.String(), nullable=True),
        sa.Column("trade_id", sa.String(), nullable=True),
        sa.Column("trade_retries", sa.Integer(), nullable=True),
        sa.Column("buys", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_problems_order_id", "order_problems", ["order_id"])
    op.create_index("ix_order_problems_kind", "order_problems", ["kind"])
    op.create_index("ix_order_problems_bot_name", "order_problems", ["bot_name"])
    op.create_index("ix_order_problems_status", "order_problems", ["status"])
    op.create_index("ix_order_problems_created_at", "order_problems", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_order_problems_created_at", table_name="order_problems")
    op.drop_index("ix_order_problems_status", table_name="order_problems")
    op.drop_index("ix_order_problems_bot_name", table_name="order_problems")
    op.drop_index("ix_order_problems_kind", table_name="order_problems")
    op.drop_index("ix_order_problems_order_id", table_name="order_problems")
    op.drop_table("order_problems")
