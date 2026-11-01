"""Background loop: refresh artists, scan the library, rebuild recommendations, notify."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from chordarr.config import settings
from chordarr.db import session_scope
from chordarr.models import Artist, utcnow
from chordarr.services import artists as artist_service
from chordarr.services import library, notify, recommendations

log = logging.getLogger(__name__)

state: dict = {"running": {}, "last": {}, "errors": {}}
_lock = asyncio.Lock()  # one heavy job at a time
_wake = asyncio.Event()


async def run_job(name: str, coro_factory) -> None:
    async with _lock:
        state["running"][name] = utcnow()
        try:
            await coro_factory()
            state["last"][name] = utcnow()
            state["errors"].pop(name, None)
        except Exception as exc:
            log.exception("job %s failed", name)
            state["errors"][name] = str(exc)[:300]
        finally:
            state["running"].pop(name, None)


async def refresh_due_artists(force: bool = False) -> None:
    cutoff = utcnow() - timedelta(hours=settings.refresh_interval_hours)
    async with session_scope() as session:
        q = select(Artist).where(Artist.monitored.is_(True))
        if not force:
            q = q.where((Artist.last_refresh.is_(None)) | (Artist.last_refresh < cutoff))
        due = (await session.scalars(q.order_by(Artist.last_refresh))).all()
        ids = [a.id for a in due]
    for artist_id in ids:
        async with session_scope() as session:
            artist = await session.get(Artist, artist_id)
            if artist is None:
                continue
            c = await artist_service.refresh_artist(session, artist, announce=True)
            await session.refresh(artist, ["releases"])
            await library.scan_artist(session, artist)
            log.info("refreshed %s: %s", artist.name, c)
        await dispatch()


async def scan_library() -> None:
    async with session_scope() as session:
        totals = await library.scan_all(session)
        log.info("library scan: %s", totals)
    await dispatch()


async def rebuild_recommendations() -> None:
    async with session_scope() as session:
        n = await recommendations.rebuild(session)
        log.info("recommendations rebuilt: %s candidates", n)


async def dispatch() -> None:
    async with session_scope() as session:
        n = await notify.dispatch_pending(session)
        if n:
            log.info("notifications sent: %s", n)


def trigger(name: str) -> asyncio.Task:
    """Run a job now, from the API/UI."""
    factories = {
        "refresh": lambda: refresh_due_artists(force=True),
        "scan": scan_library,
        "recommendations": rebuild_recommendations,
        "notify": dispatch,
    }
    return asyncio.create_task(run_job(name, factories[name]))


async def loop() -> None:
    await asyncio.sleep(5)
    tick = 0
    while True:
        try:
            if tick % 1 == 0:
                await run_job("refresh", refresh_due_artists)
            last_scan = state["last"].get("scan")
            if last_scan is None or utcnow() - last_scan > timedelta(hours=settings.scan_interval_hours):
                await run_job("scan", scan_library)
            last_reco = state["last"].get("recommendations")
            if last_reco is None or utcnow() - last_reco > timedelta(hours=settings.recommendations_interval_hours):
                await run_job("recommendations", rebuild_recommendations)
            await run_job("notify", dispatch)
        except Exception:
            log.exception("scheduler tick failed")
        tick += 1
        try:
            await asyncio.wait_for(_wake.wait(), timeout=15 * 60)
            _wake.clear()
        except TimeoutError:
            pass


def wake() -> None:
    _wake.set()


def status() -> dict:
    def fmt(d: datetime | None):
        return d.isoformat(timespec="seconds") if d else None

    return {
        "running": {k: fmt(v) for k, v in state["running"].items()},
        "last": {k: fmt(v) for k, v in state["last"].items()},
        "errors": state["errors"],
    }
