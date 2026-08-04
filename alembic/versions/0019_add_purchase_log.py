"""purchase_log — журнал выкупов, брошенных предметов и возвратов по заказу

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-31

У заказа одно поле цены (exec_price_usd), и перевыкуп его перезаписывает: бухгалтерия
видела последнюю покупку вместо всех, поэтому реальные затраты по заказам с перевыкупами
были занижены и восстановить их из базы было нельзя. Журнал хранит каждое денежное
событие отдельной строкой.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "purchase_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("kind", sa.String, nullable=False),          # buy / abandon / refund
        sa.Column("starpets_purchase_id", sa.String, nullable=True),
        sa.Column("trade_id", sa.String, nullable=True),
        sa.Column("price_usd", sa.Numeric(10, 3), nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("source", sa.String, nullable=True),         # worker / import
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=True),
    )
    op.create_index("ix_purchase_log_order_id", "purchase_log", ["order_id"])
    op.create_index("ix_purchase_log_kind", "purchase_log", ["kind"])
    op.create_index("ix_purchase_log_created_at", "purchase_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_purchase_log_created_at", table_name="purchase_log")
    op.drop_index("ix_purchase_log_kind", table_name="purchase_log")
    op.drop_index("ix_purchase_log_order_id", table_name="purchase_log")
    op.drop_table("purchase_log")
