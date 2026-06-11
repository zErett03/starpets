from fastapi import FastAPI

from app.api.webhooks import router as webhooks_router

app = FastAPI(title="starpets-layer")
app.include_router(webhooks_router)
