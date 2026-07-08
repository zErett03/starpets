import enum

from datetime import datetime

from sqlalchemy import (
    Column, Integer, BigInteger, String, Numeric, Text, DateTime,
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
    sku_product_id = Column(Integer, nullable=True)  # SKU-master: resolved product for the selected variant
    force_deliver = Column(Boolean, default=False, server_default="false", nullable=False)  # operator override: buy even at a loss
    starpets_status = Column(String, nullable=True)
    starpets_error_code = Column(String, nullable=True)

    exec_price_usd = Column(Numeric(10, 3), nullable=True)
    trade_retry_count = Column(Integer, default=0, nullable=False, server_default="0")

    delivery_status = Column(SAEnum(DeliveryStatus), nullable=False, default=DeliveryStatus.pending)
    ggsel_marked_delivered_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    # Stable anchor for the 10-min delivery timer: set to the moment the current
    # trade's friendship request was fired (dispatch / redeliver). Unlike updated_at
    # (bumped on every change), this is only reset when a NEW trade window opens.
    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    error_reason = Column(Text, nullable=True)
    last_redeliver_result = Column(Text, nullable=True)
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


class TradeEvent(Base):
    """Per-order audit trail of StarPets trade status events (for dispute defense).

    The /trades/updates stream never emits a terminal event, so we persist the full
    progression (0->2->3->4->5, plus any event:2) we DO receive. order_id is stamped at
    record time, so an order that spans multiple trades (re-deliver) keeps all of them.
    """
    __tablename__ = "trade_events"
    __table_args__ = (
        UniqueConstraint("trade_id", "sp_event_id", name="uq_trade_events_trade_event"),
    )

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)
    trade_id = Column(String, nullable=True, index=True)
    sp_event_id = Column(Integer, nullable=True)
    event_type = Column(SmallInteger, nullable=True)   # 1 = update, 2 = finished/canceled
    status = Column(SmallInteger, nullable=True)       # trade status 0..8 (None for heartbeats)
    bot_name = Column(String, nullable=True)
    data_json = Column(JSON, nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=True)   # from event data.updatedAt
    recorded_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class StoreItem(Base):
    """Mirror of StarPets store items for OUR products, kept live via the
    /ex-buyers/updates event feed. The floor of a product = the minimum price_usd
    among its items with reserve_level == 0 (FREE / actually buyable)."""
    __tablename__ = "store_items"

    id = Column(BigInteger, primary_key=True)            # StarPets store item id
    product_id = Column(Integer, index=True, nullable=False)
    price_usd = Column(Numeric(10, 3), nullable=True)
    reserve_level = Column(SmallInteger, default=0)      # 0 FREE, 1 CART, 2 PURCHASE, 3 FREEZE
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class SkuVariant(Base):
    """SKU-master (prototype): maps a ggsel radio-option variant to a StarPets product.
    One base card carries a 'Вариант' radio option; each variant = one property combo,
    resolved to its starpets_product_id at purchase time via the webhook `options` value."""
    __tablename__ = "sku_variants"

    id = Column(Integer, primary_key=True)
    ggsel_offer_id = Column(Integer, index=True, nullable=False)
    ggsel_option_id = Column(Integer, nullable=True)
    ggsel_variant_id = Column(Integer, index=True, nullable=False)
    starpets_product_id = Column(Integer, nullable=False)
    label = Column(String, nullable=True)
    price_rub = Column(Numeric(10, 2), nullable=True)
    hidden = Column(Boolean, default=False, server_default="false", nullable=False)  # out-of-stock -> archived on ggsel
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class SkuProduct(Base):
    """SKU-master product catalog mirror (from StarPets get_all_products) WITH pumping —
    the authoritative source for grouping combos into SKU cards. Unlike `offers`, it does NOT
    collapse neon/mega (which the offers composite key drops)."""
    __tablename__ = "sku_products"

    product_id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    rare = Column(String, nullable=True)
    item_type = Column(String, nullable=True)
    age = Column(String, nullable=True)
    pumping = Column(String, nullable=True)
    flyable = Column(Boolean, default=False)
    rideable = Column(Boolean, default=False)
    image_uri = Column(String, nullable=True)
    image_missing = Column(Boolean, default=False, server_default="false", nullable=False)  # StarPets 'NO IMAGE' placeholder
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
