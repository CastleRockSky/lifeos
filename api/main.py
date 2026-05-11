"""
main.py — LifeOS API Server

Slim app shell: creates the FastAPI app, wires middleware, includes routers.
All route logic lives in api/routers/*.
"""

import asyncio
import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from database import init_pool, close_pool
from search import ensure_collection

from routers import system, subjects, documents, search, actions

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    await init_pool()
    ensure_collection()

    os.makedirs(settings.upload_dir, exist_ok=True)
    os.makedirs(os.path.join(settings.upload_dir, "files"), exist_ok=True)

    # Start inbox watcher if enabled
    inbox_task = None
    if settings.inbox_enabled:
        from inbox_watcher import watch_inbox
        inbox_task = asyncio.create_task(watch_inbox())

    yield

    # Stop inbox watcher
    if inbox_task is not None:
        inbox_task.cancel()
        try:
            await inbox_task
        except asyncio.CancelledError:
            pass

    await close_pool()


app = FastAPI(title="LifeOS API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Include Routers ────────────────────────────────────────────────────
app.include_router(system.router)
app.include_router(subjects.router)
app.include_router(documents.router)
app.include_router(search.router)
app.include_router(actions.router)
