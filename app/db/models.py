import enum

from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Numeric, Text, DateTime,
    ForeignKey, SmallInteger, Enum as SAEnum, JSON, Boolean, UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class OfferStatus(str, enum.Enum):
    pending_create = "pending_create"
    draft = "draft"
    active = "active"
    paused = "paused"
    error = "error"


class DeliveryStatus(str, enum.Enum):
    pending = "pending"
    dispatched = "dispatched"
    done = "done"
    finalized = "finalized"
    failed = "failed"
    needs_attention = "needs_attention"


class TaskKind(str, enum.Enum):
    CREATE_OFFER = "CREATE_OFFER"
    UPDATE_PRICE_BATCH = "UPDATE_PRICE_BATCH"
    TOGGLE_STATUS_BATCH = "TOGGLE_STATUS_BATCH"
    DELIVER = "DELIVER"
    MONITOR_DELIVERY = "MONITOR_DELIVERY"
    MARK_DELIVERED = "MARK_DELIVERED"
    TRADE_WATCH = "TRADE_WATCH"


class TaskStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"


class WebhookKind(str, enum.Enum):
    precheck = "precheck"
    notification = "notification"


class Offer(Base):
    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint(
            "name", "item_type", "rare", "flyable", "rideable", "age",
            name="uq_offers_composite",
        ),
    )

    id = Column(Integer, primary_key=True)

    # StarPets item identity
    name = Column(String, nullable=False)
    item_type = Column(String, nullable=True)
    rare = Column(String, nullable=True)
    flyable = Column(Boolean, default=False)
    rideable = Column(Boolean, default=False)
    age = Column(String, nullable=True)

    ggsel_offer_id = Column(Integer, unique=True, nullable=True)
    ggsel_id_goods = Column(Integer, unique=True, nullable=True)
    ggsel_option_id = Column(Integer, nullable=True)

    status = Column(SAEnum(OfferStatus), nullable=False, default=OfferStatus.pending_create)

    price_usd = Column(Numeric(10, 3), nullable=True)
    price_rub = Column(Numeric(10, 2), nullable=True)
    starpets_qty = Column(Integer, default=0)
    image_uri = Column(String, nullable=True)
    starpets_product_id = Column(Integer, nullable=True)
    price_hash = Column(String(16), nullable=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)

    error_count = Column(Integer, default=0)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    orders = relationship("Order", back_populates="offer")


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    ggsel_order_id = Column(Integer, unique=True, nullable=False)
    offer_id = Column(Integer, ForeignKey("offers.id"), nullable=False)
    item_name = Column(String, nullable=False)

    amount_rub = Column(Numeric(10, 2), nullable=True)
    max_price_usd = Column(Numeric(10, 3), nullable=True)

    roblox_username = Column(String, nullable=True)
    bot_name = Column(String, nullable=True)

    buyer_email = Column(String, nullable=True)
    buyer_ip = Column(String, nullable=True)

    uniquecode = Column(String, nullable=True)

    starpets_purchase_id = Column(String, nullable=True)
    starpets_custom_id = Column(String, nullable=True)
    starpets_status = Column(String, nullable=True)
    starpets_error_code = Column(String, nullable=True)

    exec_price_usd = Column(Numeric(10, 3), nullable=True)
    trade_retry_count = Column(Integer, default=0, nullable=False, server_default="0")

    delivery_status = Column(SAEnum(DeliveryStatus), nullable=False, default=DeliveryStatus.pending)
    ggsel_marked_delivered_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    error_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    offer = relationship("Offer", back_populates="orders")


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True)
    kind = Column(SAEnum(TaskKind), nullable=False)
    priority = Column(SmallInteger, default=10)
    status = Column(SAEnum(TaskStatus), nullable=False, default=TaskStatus.pending)
    payload = Column(JSON, nullable=True)
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=5)
    scheduled_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    last_error = Column(Text, nullable=True)
    locked_by = Column(String, nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True)
    kind = Column(SAEnum(WebhookKind), nullable=False)
    external_id = Column(String, unique=True, nullable=False)
    payload = Column(JSON, nullable=True)
    response_code = Column(Integer, nullable=True)
    processed_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class GgselToken(Base):
    __tablename__ = "ggsel_tokens"

    id = Column(Integer, primary_key=True)
    seller_api_token = Column(String, nullable=True)
    seller_api_valid_thru = Column(DateTime(timezone=True), nullable=True)
    seller_office_access_token = Column(String, nullable=True)
    seller_office_refresh_token = Column(String, nullable=True)
    seller_office_expires_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class KVState(Base):
    """Tiny key/value store for durable scalar state (e.g. the trades poll cursor)."""
    __tablename__ = "kv_state"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
