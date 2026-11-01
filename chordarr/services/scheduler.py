"""Background loop: refresh artists, scan the library, rebuild recommendations, notify,
read indexer RSS feeds, search for wanted releases, follow the download queue."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from chordarr.config import settings
from chordarr.db import session_scope
from chordarr.models import Artist, utcnow
from chordarr.services import acquisition, importer, library, notify, recommendations
from chordarr.services import artists as artist_service
from chordarr.services.prefs import get_prefs

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


async def process_queue() -> None:
    async with session_scope() as session:
        c = await importer.process_queue(session)
        if c["imported"] or c["failed"]:
            log.info("queue: %s", c)
    if c["imported"] or c["failed"]:
        await dispatch()


async def rss_sync() -> None:
    async with session_scope() as session:
        c = await acquisition.rss_sync(session)
        if c["indexers"]:
            log.info("rss: %s", c)
    await dispatch()


async def search_wanted(force: bool = False) -> None:
    async with session_scope() as session:
        prefs = await get_prefs(session)
        if not prefs.auto_grab and not force:
            return
        c = await acquisition.search_wanted(session, grab_all=force)
        log.info("search: %s", c)
    await dispatch()
async def scheduled_backup() -> None:
    from chordarr.services import backup

    await backup.create("scheduled")


def backup_due() -> bool:
    """Based on the newest scheduled backup on disk, so a restart does not write one."""
    from chordarr.services import backup

    newest = backup.newest("scheduled")
    return newest is None or utcnow() - newest > timedelta(days=settings.backup_interval_days)


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
        "queue": process_queue,
        "rss": rss_sync,
        "search": lambda: search_wanted(force=True),
        "backup": lambda: scheduled_backup(),
    }
    return asyncio.create_task(run_job(name, factories[name]))


def _due(name: str, every: timedelta) -> bool:
    last = state["last"].get(name)
    return last is None or utcnow() - last > every


async def loop() -> None:
    await asyncio.sleep(5)
    while True:
        try:
            if _due("refresh", timedelta(minutes=15)):
                await run_job("refresh", refresh_due_artists)
            if _due("scan", timedelta(hours=settings.scan_interval_hours)):
                await run_job("scan", scan_library)
            if _due("recommendations", timedelta(hours=settings.recommendations_interval_hours)):
                await run_job("recommendations", rebuild_recommendations)
            if _due("rss", timedelta(minutes=settings.rss_interval_minutes)):
                await run_job("rss", rss_sync)
            if _due("search", timedelta(hours=settings.search_interval_hours)):
                await run_job("search", search_wanted)
            if _due("queue", timedelta(seconds=settings.queue_poll_seconds)):
                await run_job("queue", process_queue)
            await run_job("notify", dispatch)
            if settings.backup_interval_days and backup_due():
                await run_job("backup", scheduled_backup)
        except Exception:
            log.exception("scheduler tick failed")
        try:
            await asyncio.wait_for(_wake.wait(), timeout=min(60, settings.queue_poll_seconds))
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
