import httpx

from app.config import settings

SELLER_OFFICE_V2_URL = settings.ggsel_base_url


class GgselSellerOfficeClient:
    """Seller Office API v2 — создание офферов, управление, доставка."""

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": settings.ggsel_api_key,
        }

    async def create_offer(
        self,
        title_ru: str,
        title_en: str,
        description_ru: str,
        description_en: str,
        instructions_ru: str,
        instructions_en: str,
        category_id: int,
        cover_base64: str,
        price: float,
        cover_mime: str = "image/jpeg",
    ) -> dict:
        cover_data_uri = f"data:{cover_mime};base64,{cover_base64}" if cover_base64 else None
        body = {
            "title_ru": title_ru,
            "title_en": title_en,
            "description_ru": description_ru,
            "description_en": description_en,
            "instructions_ru": instructions_ru,
            "instructions_en": instructions_en,
            "cover_image_ru": cover_data_uri,
            "price": price,
            "currency": "RUB",
            "quantity": 999,
            "is_autoselling": False,
            "category_id": category_id,
            "delivery": "manual",
            "post_payment_url": f"{settings.public_url}/delivery",
        }

        import json as _json

        print(f"[create_offer] body: {_json.dumps(body, ensure_ascii=False)}", flush=True)

        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await client.post(f"{SELLER_OFFICE_V2_URL}/offers", json=body)
            print(f"[create_offer] status={resp.status_code} response={resp.text[:500]}", flush=True)
            if not resp.is_success:
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} {resp.text}",
                    request=resp.request,
                    response=resp,
                )
            return resp.json()

    async def patch_offer(self, offer_id: int, precheck_url: str, notification_url: str) -> dict:
        body = {
            "pre_payment_settings": {
                "is_enabled": True,
                "url": precheck_url,
                "allow_payment": True,
            },
            "notification_settings": {
                "type": "url",
                "url": notification_url,
                "http_method": "POST",
                "is_disabled": False,
                "is_default": False,
            },
        }

        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await client.patch(f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}", json=body)
            print(f"[patch_offer] status={resp.status_code} response={resp.text[:500]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    async def create_option(self, offer_id: int) -> dict:
        body = {
            "options": [
                {
                    "type": "text",
                    "title_ru": "Ваш Roblox Username",
                    "title_en": "Your Roblox Username",
                    "comment_ru": "Имя пользователя в Roblox для отправки трейда",
                    "is_required": True,
                    "position": 1,
                }
            ]
        }

        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await client.post(f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options", json=body)
            print(f"[create_option] status={resp.status_code} response={resp.text[:500]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    async def get_options(self, offer_id: int) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=10) as client:
            resp = await client.get(f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options")
            resp.raise_for_status()
            return resp.json()

    async def delete_options(self, offer_id: int, option_ids: list[int]) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=10) as client:
            resp = await client.request(
                "DELETE",
                f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options",
                json={"option_ids": option_ids, "delete_all": "false"},
            )
            resp.raise_for_status()
            return resp.json()

    async def set_post_payment_url(self, offer_id: int, url: str) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await client.patch(
                f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}",
                json={"post_payment_url": url},
            )
            resp.raise_for_status()
            return resp.json()

    async def update_price(self, offer_id: int, price: float) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await client.patch(
                f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}",
                json={"price": price},
            )
            resp.raise_for_status()
            return resp.json()

    async def activate_offer(self, offer_id: int) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=10) as client:
            resp = await client.post(
                f"{SELLER_OFFICE_V2_URL}/offers/batch_activate",
                json={"offer_ids": [offer_id]},
            )
            print(f"[activate_offer] status={resp.status_code} response={resp.text[:500]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    async def batch_activate(self, offer_ids: list[int]) -> dict:
        """POST /offers/batch_activate — activate up to 100 offers at once."""
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await client.post(
                f"{SELLER_OFFICE_V2_URL}/offers/batch_activate",
                json={"offer_ids": offer_ids},
            )
            print(
                f"[batch_activate] count={len(offer_ids)} status={resp.status_code} "
                f"response={resp.text[:200]}",
                flush=True,
            )
            resp.raise_for_status()
            return resp.json()

    async def pause_offers(self, offer_ids: list[int]) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=10) as client:
            resp = await client.post(
                f"{SELLER_OFFICE_V2_URL}/offers/batch/actions/pause",
                json={"offer_ids": offer_ids},
            )
            resp.raise_for_status()
            return resp.json()

    async def activate_offers(self, offer_ids: list[int]) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=10) as client:
            resp = await client.post(
                f"{SELLER_OFFICE_V2_URL}/offers/batch/actions/activate",
                json={"offer_ids": offer_ids},
            )
            resp.raise_for_status()
            return resp.json()

    async def mark_delivered(self, order_id: int) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=10) as client:
            resp = await client.post(
                f"{SELLER_OFFICE_V2_URL}/orders/{order_id}/deliveries/delivered"
            )
            resp.raise_for_status()
            return resp.json()

    async def get_order(self, order_id: int) -> dict:
        async with httpx.AsyncClient(
            headers={**self._headers(), "currency": "RUB"}, timeout=10
        ) as client:
            resp = await client.get(f"{SELLER_OFFICE_V2_URL}/orders/{order_id}")
            resp.raise_for_status()
            return resp.json()


ggsel_office = GgselSellerOfficeClient()
