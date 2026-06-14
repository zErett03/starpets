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


@app.get("/db-stats")
async def db_stats():
    from sqlalchemy import func, select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(func.count()).select_from(Offer))
        count = result.scalar()
    return {"offers": count}


@app.get("/test-sync")
async def test_sync():
    from app.scheduler.jobs import starpets_sync
    await starpets_sync()
    from sqlalchemy import func, select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(func.count()).select_from(Offer))
        count = result.scalar()
    return {"offers_in_db": count}


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
