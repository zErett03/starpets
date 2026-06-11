import httpx

from datetime import datetime, timedelta

_cache: dict = {"rate": None, "valid_until": None}


async def get_usd_rub() -> float:
    now = datetime.utcnow()

    if _cache["rate"] and _cache["valid_until"] and now < _cache["valid_until"]:
        return _cache["rate"]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://www.cbr-xml-daily.ru/daily_json.js")
            resp.raise_for_status()
            data = resp.json()
            rate = data["Valute"]["USD"]["Value"]
            _cache["rate"] = rate
            _cache["valid_until"] = now + timedelta(hours=1)
            print(f"[FX] USD/RUB = {rate}")
            return rate
    except Exception as e:
        print(f"[FX] Failed to get rate: {e}")
        if _cache["rate"]:
            return _cache["rate"]
        raise RuntimeError("FX rate unavailable") from e


def calc_price_rub(price_usd: float, markup: float, fx_rate: float) -> float:
    from app.config import settings

    price = round(price_usd * markup * fx_rate, 2)
    return max(settings.min_price_rub, price)
