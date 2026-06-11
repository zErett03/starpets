import asyncio

import uvicorn

from app.workers.task_runner import run_worker
from app.scheduler.jobs import start_scheduler


async def main():
    start_scheduler()

    config = uvicorn.Config(
        "app.api:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
    server = uvicorn.Server(config)

    await asyncio.gather(
        server.serve(),
        run_worker(),
    )


if __name__ == "__main__":
    asyncio.run(main())
