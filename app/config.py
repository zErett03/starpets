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

    webhook_shared_secret: str

    database_url: str

    telegram_bot_token: str = "dummy"
    telegram_chat_id_critical: str = ""
    telegram_chat_id_warn: str = ""

    public_url: str = "https://starpets-production.up.railway.app"

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
    starpets_category_id: int = 0

    class Config:
        env_file = ".env"


settings = Settings()
