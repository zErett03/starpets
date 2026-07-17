from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    starpets_api_key: str
    starpets_secret: str
    starpets_shared_key: str
    starpets_account_id: str
    starpets_base_url: str = "https://market.neuralgeneration.com/api"

    ggsel_api_key: str
    ggsel_base_url: str = "https://seller.ggsel.com/api_sellers/v2"
    ggsel_access_token: str = ""
    ggsel_qrator: str = ""
    # Token for the legacy purchase API (/api_sellers/api/purchases/*) used by the delivery-page
    # uniquecode resolver. It uses ?token=... (NOT the Bearer Authorization header of v2). If empty
    # we fall back to ggsel_api_key. Set GGSEL_PURCHASE_TOKEN if the api key is not accepted here.
    ggsel_purchase_token: str = ""
    # apilogin flow for the legacy purchase API: POST /api_sellers/api/apilogin with
    # {seller_id, timestamp, sign=SHA256(sign_key+timestamp)} -> short-lived token (~2h).
    ggsel_seller_id: int = 0              # your ggsel/Digiseller seller id (GGSEL_SELLER_ID)
    ggsel_purchase_api_key: str = ""      # key used to SIGN apilogin; empty -> ggsel_api_key

    webhook_shared_secret: str

    database_url: str

    telegram_bot_token: str = "dummy"
    telegram_chat_id_critical: str = ""
    telegram_chat_id_warn: str = ""
    telegram_chat_id_orders: str = ""     # new-order + problem alerts go here (default: first admin id)
    telegram_admin_ids: str = ""          # comma-separated Telegram user ids allowed to use the bot
    telegram_webhook_secret: str = ""     # secret path segment for /telegram/webhook/<secret>
    mm2_public_url: str = ""              # MM2 Railway URL — router relays _mm2 commands here
    mm2_relay_secret: str = ""            # = MM2 TELEGRAM_WEBHOOK_SECRET (gates MM2 /telegram/exec)
    maintenance_message: str = ""         # if set, precheck blocks ALL sales and shows this text to buyers

    public_url: str = "https://starpets-production.up.railway.app"
    delivery_base_url: str = ""            # buyer-facing delivery page domain (RU VPS). Webhooks stay on public_url; only /delivery uses this. Empty -> public_url.

    # Operator admin panel (Basic Auth). Set ADMIN_PASSWORD in env to enable.
    # If admin_password is empty the panel is fail-closed (denies every request).
    admin_user: str = "admin"
    admin_password: str = ""

    markup: float = 1.20
    # Profitability guard: refuse to buy an item whose live cost (raw price × FX, no
    # markup) exceeds this fraction of the sale price. 0.9 = never spend >90% of what the
    # buyer paid on the item, leaving ≥10% for ggsel commission + margin. Protects against
    # unprofitable trades when the live floor spiked above our (30-min-stale) offer price.
    max_cost_ratio: float = 0.9
    # Price-sync parallelism: how many offers to process concurrently. Higher = faster
    # sync but more load / risk of 429 from StarPets/ggsel. Tune via env SYNC_CONCURRENCY.
    sync_concurrency: int = 10
    # Per-offer price-sync logging. False (default) logs only progress + final summary
    # + errors (keeps Railway logs clean). Set SYNC_LOG_VERBOSE=true to see every offer.
    sync_log_verbose: bool = False
    min_price_rub: float = 100.0

    # When True: prices are kept live by the event-driven price_sync worker (reads the
    # StarPets /ex-buyers/updates feed) and the legacy 30-min top-per-product sync_prices
    # is disabled. Flip via env EVENT_PRICE_SYNC after seeding store_items.
    event_price_sync: bool = False
    sku_price_sync: bool = False   # Phase 3: periodic SKU variant price refresh (SKU_PRICE_SYNC=true)
    sku_stock_sync: bool = False   # hide out-of-stock SKU variants (SKU_STOCK_SYNC=true)
    floor_reconcile: bool = False  # sweep offers.price_rub from store_items + live relive (FLOOR_RECONCILE=true)
    starpets_category_id: int = 0

    class Config:
        env_file = ".env"


settings = Settings()
