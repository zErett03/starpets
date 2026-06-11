from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    starpets_api_key: str
    starpets_secret: str
    starpets_base_url: str = "https://market.neuralgeneration.com/api"

    ggsel_api_key: str
    ggsel_access_token: str = ""
    ggsel_qrator: str = ""

    webhook_shared_secret: str

    database_url: str

    telegram_bot_token: str = "dummy"
    telegram_chat_id_critical: str = ""
    telegram_chat_id_warn: str = ""

    markup: float = 1.20
    min_price_rub: float = 100.0
    starpets_category_id: int = 0

    class Config:
        env_file = ".env"


settings = Settings()
