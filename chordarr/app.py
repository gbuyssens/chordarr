"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from chordarr import __version__
from chordarr.api.acquisition import router as acquisition_router
from chordarr.api.feed import router as feed_router
from chordarr.api.lidarr import router as lidarr_router
from chordarr.api.lidarr_compat import LIDARR_API_VERSION
from chordarr.api.router import router as api_router
from chordarr.api.ui import router as ui_router
from chordarr.config import settings
from chordarr.db import init_db
from chordarr.services import scheduler

log = logging.getLogger("chordarr")
HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_db()
    log.info("Chordarr %s · data in %s · root folder %s", __version__, settings.data_dir, settings.root_folder)
    task = asyncio.create_task(scheduler.loop())
    try:
        yield
    finally:
        task.cancel()


def create_app() -> FastAPI:
    app = FastAPI(title="Chordarr", version=__version__, lifespan=lifespan, root_path=settings.url_base)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(api_router)
    app.include_router(acquisition_router)
    app.include_router(lidarr_router)
    app.include_router(feed_router)
    app.include_router(ui_router)

    @app.middleware("http")
    async def version_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(f"{settings.url_base}/api/"):
            # Prowlarr reads X-Application-Version on its "Lidarr" connection test.
            response.headers.setdefault("X-Application-Version", LIDARR_API_VERSION)
            response.headers["X-Chordarr-Version"] = __version__
        return response

    return app


app = create_app()
