"""initial

Revision ID: 0001
Revises:
Create Date: 2026-06-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

offerstatus = sa.Enum(
    "pending_create", "draft", "active", "paused", "error",
    name="offerstatus",
)
deliverystatus = sa.Enum(
    "pending", "dispatched", "done", "finalized", "failed", "needs_attention",
    name="deliverystatus",
)
taskkind = sa.Enum(
    "CREATE_OFFER", "UPDATE_PRICE_BATCH", "TOGGLE_STATUS_BATCH",
    "DELIVER", "MONITOR_DELIVERY", "MARK_DELIVERED", "TRADE_WATCH",
    name="taskkind",
)
taskstatus = sa.Enum(
    "pending", "processing", "done", "failed",
    name="taskstatus",
)
webhookkind = sa.Enum(
    "precheck", "notification",
    name="webhookkind",
)


def upgrade() -> None:
    offerstatus.create(op.get_bind(), checkfirst=True)
    deliverystatus.create(op.get_bind(), checkfirst=True)
    taskkind.create(op.get_bind(), checkfirst=True)
    taskstatus.create(op.get_bind(), checkfirst=True)
    webhookkind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "offers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, unique=True, nullable=False),
        sa.Column("item_type", sa.String, nullable=True),
        sa.Column("rare", sa.String, nullable=True),
        sa.Column("flyable", sa.Boolean, default=False),
        sa.Column("rideable", sa.Boolean, default=False),
        sa.Column("age", sa.String, nullable=True),
        sa.Column("ggsel_offer_id", sa.Integer, unique=True, nullable=True),
        sa.Column("ggsel_id_goods", sa.Integer, unique=True, nullable=True),
        sa.Column("ggsel_option_id", sa.Integer, nullable=True),
        sa.Column("status", offerstatus, nullable=False, server_default="pending_create"),
        sa.Column("price_usd", sa.Numeric(10, 3), nullable=True),
        sa.Column("price_rub", sa.Numeric(10, 2), nullable=True),
        sa.Column("starpets_qty", sa.Integer, default=0),
        sa.Column("price_hash", sa.String(16), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_count", sa.Integer, default=0),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ggsel_order_id", sa.Integer, unique=True, nullable=False),
        sa.Column("offer_id", sa.Integer, sa.ForeignKey("offers.id"), nullable=False),
        sa.Column("item_name", sa.String, nullable=False),
        sa.Column("amount_rub", sa.Numeric(10, 2), nullable=True),
        sa.Column("max_price_usd", sa.Numeric(10, 3), nullable=True),
        sa.Column("roblox_username", sa.String, nullable=True),
        sa.Column("buyer_email", sa.String, nullable=True),
        sa.Column("buyer_ip", sa.String, nullable=True),
        sa.Column("starpets_purchase_id", sa.String, nullable=True),
        sa.Column("starpets_custom_id", sa.String, nullable=True),
        sa.Column("starpets_status", sa.String, nullable=True),
        sa.Column("starpets_error_code", sa.String, nullable=True),
        sa.Column("exec_price_usd", sa.Numeric(10, 3), nullable=True),
        sa.Column("delivery_status", deliverystatus, nullable=False, server_default="pending"),
        sa.Column("ggsel_marked_delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", taskkind, nullable=False),
        sa.Column("priority", sa.SmallInteger, default=10),
        sa.Column("status", taskstatus, nullable=False, server_default="pending"),
        sa.Column("payload", sa.JSON, nullable=True),
        sa.Column("attempts", sa.Integer, default=0),
        sa.Column("max_attempts", sa.Integer, default=5),
        sa.Column("scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("locked_by", sa.String, nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", webhookkind, nullable=False),
        sa.Column("external_id", sa.String, unique=True, nullable=False),
        sa.Column("payload", sa.JSON, nullable=True),
        sa.Column("response_code", sa.Integer, nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "ggsel_tokens",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("seller_api_token", sa.String, nullable=True),
        sa.Column("seller_api_valid_thru", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seller_office_access_token", sa.String, nullable=True),
        sa.Column("seller_office_refresh_token", sa.String, nullable=True),
        sa.Column("seller_office_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("ggsel_tokens")
    op.drop_table("webhook_events")
    op.drop_table("tasks")
    op.drop_table("orders")
    op.drop_table("offers")
    offerstatus.drop(op.get_bind(), checkfirst=True)
    deliverystatus.drop(op.get_bind(), checkfirst=True)
    taskkind.drop(op.get_bind(), checkfirst=True)
    taskstatus.drop(op.get_bind(), checkfirst=True)
    webhookkind.drop(op.get_bind(), checkfirst=True)
