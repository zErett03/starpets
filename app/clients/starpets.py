import hashlib
import hmac
import time
import httpx

from app.config import settings


class StarPetsClient:
    def __init__(self):
        self.base_url = settings.starpets_base_url

    def _headers(self) -> dict:
        return {
            "Api_Key": settings.starpets_api_key,
            "X-Api-Key": settings.starpets_api_key,
            "Content-Type": "application/json",
        }

    def _sign(self, params: dict) -> str:
        parts = []
        for key in sorted(params.keys()):
            if key == "sign":
                continue
            if isinstance(params[key], (dict, list)):
                continue
            parts.append(f"{key}:{params[key]}")
        message = ";".join(parts) + ";"
        return hmac.new(
            settings.starpets_secret.encode(),
            message.encode(),
            hashlib.sha512,
        ).hexdigest()

    def _base_params(self) -> dict:
        return {
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000,
        }

    async def get_info(self) -> dict:
        params = self._base_params()
        params["sign"] = self._sign(params)
        async with httpx.AsyncClient(headers=self._headers(), timeout=10) as client:
            resp = await client.get(f"{self.base_url}/info", params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_products(self) -> dict:
        params = self._base_params()
        params["sign"] = self._sign(params)
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await client.get(f"{self.base_url}/products", params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_items(self, item_ids: list[str] = None) -> dict:
        params = self._base_params()
        if item_ids:
            params["item_ids"] = ",".join(item_ids)
        params["sign"] = self._sign(params)
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await client.get(f"{self.base_url}/items", params=params)
            resp.raise_for_status()
            return resp.json()

    async def buy_items(
        self,
        item_id: str,
        max_price_usd: float,
        custom_id: str,
    ) -> dict:
        payload = {
            **self._base_params(),
            "item_id": item_id,
            "max_price_usd": max_price_usd,
            "custom_id": custom_id,
        }
        payload["sign"] = self._sign(payload)
        print(f"[buy_items] payload: {payload}", flush=True)
        async with httpx.AsyncClient(headers=self._headers(), timeout=10) as client:
            resp = await client.post(f"{self.base_url}/buy", json=payload)
            print(f"[buy_items] status={resp.status_code} response={resp.text[:500]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    async def create_trade(
        self,
        purchase_id: str,
        roblox_username: str,
    ) -> dict:
        payload = {
            **self._base_params(),
            "purchase_id": purchase_id,
            "roblox_username": roblox_username,
        }
        payload["sign"] = self._sign(payload)
        print(f"[create_trade] payload: {payload}", flush=True)
        async with httpx.AsyncClient(headers=self._headers(), timeout=10) as client:
            resp = await client.post(f"{self.base_url}/trade", json=payload)
            print(f"[create_trade] status={resp.status_code} response={resp.text[:500]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    async def get_trade_updates(self, custom_id: str) -> dict:
        params = {
            **self._base_params(),
            "custom_id": custom_id,
        }
        params["sign"] = self._sign(params)
        async with httpx.AsyncClient(headers=self._headers(), timeout=10) as client:
            resp = await client.get(f"{self.base_url}/trade/status", params=params)
            resp.raise_for_status()
            return resp.json()


starpets = StarPetsClient()
