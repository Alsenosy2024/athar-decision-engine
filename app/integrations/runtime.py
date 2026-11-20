"""Initialize integration storage and run the opt-in webhook worker."""
import asyncio
from contextlib import asynccontextmanager

from app.integrations import alerts, credentials, webhooks


@asynccontextmanager
async def integration_runtime(application, settings):
    application.state.embed_secret, application.state.frame_ancestors = credentials.from_env()
    destinations = webhooks.from_env()
    credentials.initialize(settings.db_path)
    alerts.initialize(settings.db_path)
    webhooks.initialize(settings.db_path, destinations)

    stop = asyncio.Event()
    worker = asyncio.create_task(webhooks.worker(settings, destinations, stop)) if destinations else None
    try:
        yield
    finally:
        if worker is not None:
            stop.set()
            await worker
