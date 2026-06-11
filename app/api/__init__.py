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


@app.get("/test-starpets")
async def test_starpets():
    params = starpets._base_params()
    sign_input = ";".join(
        f"{k}={params[k]}" for k in sorted(params.keys())
        if not isinstance(params[k], (dict, list))
    )
    params["sign"] = starpets._sign(params)
    async with httpx.AsyncClient(headers=starpets._headers(), timeout=10) as client:
        resp = await client.get(
            f"{starpets.base_url}/ex-buyers/info/me",
            params=params,
        )
        return {
            "status_code": resp.status_code,
            "sign_input": sign_input,
            "body": resp.json(),
        }
