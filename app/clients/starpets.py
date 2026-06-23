import hashlib
import hmac
import json as _json
import time
import httpx

from app.config import settings


class StarPetsClient:
    def __init__(self):
        self.base_url = settings.starpets_base_url

    def _sign(self, params: dict) -> str:
        parts = []
        for k, v in params.items():
            if isinstance(v, (list, dict)):
                parts.append(f"{k}:{_json.dumps(v, separators=(',', ':'))}")
            else:
                parts.append(f"{k}:{v}")
        qs = ";".join(parts) + ";"
        return hmac.new(
            settings.starpets_secret.encode(),
            qs.encode(),
            hashlib.sha512,
        ).hexdigest()

    def _headers(self, signature: str) -> dict:
        return {
            "Api_Key": settings.starpets_shared_key,
            "X-Api-Key": settings.starpets_account_id,
            "Signature": signature,
            "Content-Type": "application/json",
        }

    def _base_params(self) -> dict:
        return {
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000,
        }

    async def get_info(self) -> dict:
        params = self._base_params()
        async with httpx.AsyncClient(headers=self._headers(self._sign(params)), timeout=10) as client:
            resp = await client.get(f"{self.base_url}/info", params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_all_products(self) -> list:
        all_items = []
        cursor = None
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                params = {**self._base_params(), "limit": 500}
                if cursor:
                    params["cursor"] = cursor
                resp = await client.get(
                    f"{self.base_url}/products/ex-buyers/all-by-cursor",
                    headers=self._headers(self._sign(params)),
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("products") or []
                all_items.extend(items)
                if len(items) < 500:
                    break
                cursor = items[-1]["id"]
        return all_items

    async def get_products(self) -> dict:
        params = self._base_params()
        async with httpx.AsyncClient(headers=self._headers(self._sign(params)), timeout=30) as client:
            resp = await client.get(f"{self.base_url}/products", params=params)
            resp.raise_for_status()
            return resp.json()

    async def iter_items(self):
        """Async generator yielding pages of 1000 items without accumulating all in memory."""
        cursor = 0
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                params = {**self._base_params(), "limit": 1000, "cursor": cursor}
                resp = await client.get(
                    f"{self.base_url}/store/ex-buyers/items/all",
                    headers=self._headers(self._sign(params)),
                    params=params,
                )
                if not resp.is_success:
                    raise RuntimeError(
                        f"iter_items {resp.status_code}: {resp.text}"
                    )
                data = resp.json()
                items = data.get("items") or []
                if items:
                    yield items
                if len(items) < 500:
                    break
                cursor = items[-1]["id"]

    async def get_all_items(self) -> list:
        all_items = []
        async for page in self.iter_items():
            all_items.extend(page)
        return all_items

    async def get_top_item(self, client: httpx.AsyncClient, product_id: str) -> dict | None:
        params = self._base_params()
        resp = await client.get(
            f"{self.base_url}/store/ex-buyers/items/top/{product_id}",
            headers=self._headers(self._sign(params)),
            params=params,
        )
        if not resp.is_success:
            return None
        items = resp.json().get("items") or []
        return items[0] if items else None

    async def get_items(self, item_ids: list[str] = None) -> dict:
        params = self._base_params()
        if item_ids:
            params["item_ids"] = ",".join(item_ids)
        async with httpx.AsyncClient(headers=self._headers(self._sign(params)), timeout=30) as client:
            resp = await client.get(f"{self.base_url}/items", params=params)
            resp.raise_for_status()
            return resp.json()

    async def buy_by_items(self, items: list[dict]) -> dict:
        """POST /store/ex-buyers/items/buy — buy specific items by ID at known price.

        items: [{"id": <item_id>, "price": <price_usd>}, ...]
        Response items[].id are the purchased item IDs to use in create_trade.
        """
        base = self._base_params()
        payload = {**base, "items": items}
        async with httpx.AsyncClient(
            headers=self._headers(self._sign(payload)), timeout=15
        ) as client:
            resp = await client.post(
                f"{self.base_url}/store/ex-buyers/items/buy", json=payload
            )
            resp.raise_for_status()
            return resp.json()

    async def buy_by_product(self, product_id: int, max_price_usd: float) -> dict:
        """POST /store/ex-buyers/products/buy — buy cheapest item for a product up to max price.

        Response items[].id are the purchased item IDs to use in create_trade.
        """
        base = self._base_params()
        payload = {**base, "products": [{"id": product_id, "maxPrice": max_price_usd}]}
        async with httpx.AsyncClient(
            headers=self._headers(self._sign(payload)), timeout=15
        ) as client:
            resp = await client.post(
                f"{self.base_url}/store/ex-buyers/products/buy", json=payload
            )
            resp.raise_for_status()
            return resp.json()

    async def create_trade(
        self,
        purchased_item_ids: list[str],
        roblox_username: str,
    ) -> dict:
        """POST /trades/ex-buyers/withdrawal — send purchased items to Roblox user.

        purchased_item_ids: list of string IDs from buy response items[].id
        """
        base = self._base_params()
        payload = {
            **base,
            "username": roblox_username,
            "items": purchased_item_ids,
        }
        async with httpx.AsyncClient(
            headers=self._headers(self._sign(payload)), timeout=15
        ) as client:
            resp = await client.post(
                f"{self.base_url}/trades/ex-buyers/withdrawal", json=payload
            )
            resp.raise_for_status()
            return resp.json()

    async def get_trade_updates(self, custom_id: str) -> dict:
        params = {
            **self._base_params(),
            "custom_id": custom_id,
        }
        async with httpx.AsyncClient(headers=self._headers(self._sign(params)), timeout=10) as client:
            resp = await client.get(f"{self.base_url}/trade/status", params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_bulk_trade_updates(self, limit: int = 50) -> list:
        """GET /ex-buyers/trades/updates — bulk status poll, returns list of trade objects."""
        params = {**self._base_params(), "limit": limit}
        async with httpx.AsyncClient(headers=self._headers(self._sign(params)), timeout=15) as client:
            resp = await client.get(f"{self.base_url}/ex-buyers/trades/updates", params=params)
            resp.raise_for_status()
            data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("trades") or data.get("updates") or data.get("items") or data.get("data") or []

    async def send_friendship(self, trade_id: int, item_id: str, username: str) -> dict:
        """PUT /trades/ex-buyers/friendship — ask buyer to add bot as friend."""
        params = {
            **self._base_params(),
            "tradeId": trade_id,
            "itemId": item_id,
            "username": username,
        }
        async with httpx.AsyncClient(headers=self._headers(self._sign(params)), timeout=15) as client:
            resp = await client.put(
                f"{self.base_url}/trades/ex-buyers/friendship", params=params
            )
            resp.raise_for_status()
            return resp.json()


starpets = StarPetsClient()
