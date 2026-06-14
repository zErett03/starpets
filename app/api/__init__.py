import httpx
from fastapi import FastAPI

from app.api.webhooks import router as webhooks_router
from app.clients.starpets import starpets

app = FastAPI(title="starpets-layer")
app.include_router(webhooks_router)


@app.get("/")
async def status():
    return {"status": "ok"}


@app.get("/myip")
async def myip():
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get("https://api.ipify.org?format=json")
        resp.raise_for_status()
        return resp.json()


@app.get("/test-sync")
async def test_sync():
    params = {**starpets._base_params(), "limit": 1}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{starpets.base_url}/products/ex-buyers/all-by-cursor",
            headers=starpets._headers(starpets._sign(params)),
            params=params,
        )
        first_page = resp.json()

    all_items = await starpets.get_all_products()
    return {
        "total": len(all_items),
        "first_page_raw": first_page,
    }


@app.get("/test-products")
async def test_products():
    params = {
        **starpets._base_params(),
        "limit": 5,
    }
    async with httpx.AsyncClient(headers=starpets._headers(starpets._sign(params)), timeout=10) as client:
        resp = await client.get(
            f"{starpets.base_url}/products/ex-buyers/all-by-cursor",
            params=params,
        )
        return {
            "status_code": resp.status_code,
            "body": resp.json(),
        }


@app.get("/test-starpets")
async def test_starpets():
    params = starpets._base_params()
    async with httpx.AsyncClient(headers=starpets._headers(starpets._sign(params)), timeout=10) as client:
        resp = await client.get(
            f"{starpets.base_url}/ex-buyers/info/me",
            params=params,
        )
        return {
            "status_code": resp.status_code,
            "body": resp.json(),
        }
