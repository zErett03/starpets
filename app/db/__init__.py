from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import create_engine

from app.config import settings

async_url = (
    settings.database_url
    .replace("postgresql://", "postgresql+asyncpg://")
    .replace("postgresql+psycopg2://", "postgresql+asyncpg://")
)
sync_url = (
    settings.database_url
    .replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    .replace("postgresql://", "postgresql+psycopg2://")
)

engine = create_async_engine(async_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

sync_engine = create_engine(sync_url, echo=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
