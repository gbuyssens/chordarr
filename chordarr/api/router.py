"""JSON API under /api/v1."""

from __future__ import annotations

import asyncio
import logging
import platform
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from chordarr import __version__
from chordarr.api import lidarr, schemas
from chordarr.api.auth import require_api_key
from chordarr.config import settings
from chordarr.db import get_session
from chordarr.metadata import musicbrainz as mb
from chordarr.models import Artist, Event, Notifier, Recommendation, Release
from chordarr.services import artists as artist_service
from chordarr.services import library, lidarr_import, notify, scheduler
from chordarr.services.events import EVENT_TYPES

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


def artist_out(a: Artist) -> dict:
    """Chordarr's fields plus Lidarr's, so both kinds of client read what they expect."""
    out = schemas.ArtistOut.model_validate(a)
    out.release_count = sum(1 for r in a.releases if r.kind == "own")
    out.appearance_count = sum(1 for r in a.releases if r.kind == "appearance")
    out.have_count = sum(1 for r in a.releases if r.status == "have")
    return {**lidarr.artist_payload(a), **out.model_dump(mode="json")}


# --- system -----------------------------------------------------------------------------


@router.get("/system/status")
async def system_status():
    return {
        "appName": "Chordarr",
        "instanceName": "Chordarr",
        "version": __version__,
        "buildTime": None,
        "isDebug": False,
        "isProduction": True,
        "isDocker": False,
        "runtimeVersion": platform.python_version(),
        "osName": platform.system(),
        "appData": str(settings.data_dir),
        "urlBase": settings.url_base,
        "authentication": "apikey",
        "scheduler": scheduler.status(),
    }


@router.get("/health")
async def health(session: AsyncSession = Depends(get_session)):
    issues = []
    if not settings.root_folder:
        issues.append({"type": "warning", "message": "No root folder configured (CHORDARR_ROOT_FOLDER)."})
    for folder in library.unwritable_folders():
        issues.append({"source": "RootFolderCheck", "type": "error", "message": f"Folder not writable by Chordarr: {folder}. Imports into it fail; fix the owner or give the group write access (chmod g+w).", "wikiUrl": ""})
    errored = (await session.scalars(select(Artist).where(Artist.refresh_error.is_not(None)))).all()
    for a in errored:
        issues.append({"type": "error", "message": f"{a.name}: {a.refresh_error}"})
    for name, err in scheduler.status()["errors"].items():
        issues.append({"type": "error", "message": f"job {name}: {err}"})
    return issues


@router.post("/command/{name}")
async def command(name: str):
    if name not in ("refresh", "scan", "recommendations", "notify", "rss", "search", "queue"):
        raise HTTPException(404, "unknown command")
    scheduler.trigger(name)
    return {"name": name, "status": "queued"}


# --- artists ----------------------------------------------------------------------------


@router.get("/artist/lookup")
async def artist_lookup(term: str = Query(min_length=1)):
    results = await mb.search_artists(term)
    return [
        {
            "foreignArtistId": r.mbid,
            "mbid": r.mbid,
            "artistName": r.name,
            "name": r.name,
            "disambiguation": r.disambiguation,
            "type": r.type,
            "country": r.country,
            "score": r.score,
        }
        for r in results
    ]


@router.get("/artist")
async def list_artists(session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Artist).order_by(Artist.sort_name))).all()
    return [artist_out(a) for a in rows]


@router.post("/artist", status_code=201)
async def create_artist(body: schemas.ArtistIn, session: AsyncSession = Depends(get_session)):
    if not body.artist_mbid:
        raise HTTPException(422, "mbid (or foreignArtistId) is required")
    if body.monitor_mode not in artist_service.MONITOR_MODES:
        raise HTTPException(422, f"monitor must be one of {', '.join(artist_service.MONITOR_MODES)}")
    try:
        artist = await artist_service.add_artist(
            session,
            body.artist_mbid,
            path=body.path,
            monitored=body.monitored,
            monitor_types=body.monitor_types,
            monitor_appearances=body.monitor_appearances,
            monitor=body.monitor_mode,
            monitor_new_items=body.monitorNewItems,
        )
    except artist_service.ArtistExists as exc:
        raise HTTPException(409, f"{exc} is already tracked") from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    await session.commit()
    scheduler.wake()
    return artist_out(artist)


@router.get("/artist/{artist_id}")
async def get_artist(artist_id: int, session: AsyncSession = Depends(get_session)):
    a = await session.get(Artist, artist_id)
    if not a:
        raise HTTPException(404)
    return artist_out(a)


@router.patch("/artist/{artist_id}")
async def patch_artist(artist_id: int, body: schemas.ArtistPatch, session: AsyncSession = Depends(get_session)):
    a = await session.get(Artist, artist_id)
    if not a:
        raise HTTPException(404)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(a, k, v)
    await session.commit()
    return artist_out(a)


@router.put("/artist/{artist_id}")
async def put_artist(artist_id: int, body: dict, session: AsyncSession = Depends(get_session)):
    """Lidarr clients edit with PUT and the full object; only the editable fields are read."""
    a = await session.get(Artist, artist_id)
    if not a:
        raise HTTPException(404)
    if "monitored" in body:
        a.monitored = bool(body["monitored"])
    if body.get("monitorNewItems") in ("all", "none"):
        a.monitor_new_items = body["monitorNewItems"]
    if body.get("path"):
        a.path = body["path"]
    for k in ("monitor_types", "monitor_appearances"):
        if k in body:
            setattr(a, k, body[k])
    await session.commit()
    return artist_out(a)


@router.delete("/artist/{artist_id}", status_code=204)
async def delete_artist(artist_id: int, session: AsyncSession = Depends(get_session)):
    a = await session.get(Artist, artist_id)
    if a:
        await session.delete(a)
        await session.commit()


@router.post("/artist/{artist_id}/refresh")
async def refresh_artist(artist_id: int, session: AsyncSession = Depends(get_session)):
    a = await session.get(Artist, artist_id)
    if not a:
        raise HTTPException(404)
    counters = await artist_service.refresh_artist(session, a, announce=True)
    await session.refresh(a, ["releases"])
    await library.scan_artist(session, a)
    await session.commit()
    scheduler.trigger("notify")
    return counters


@router.get("/artist/{artist_id}/release", response_model=list[schemas.ReleaseOut])
async def artist_releases(artist_id: int, kind: str | None = None, session: AsyncSession = Depends(get_session)):
    q = select(Release).where(Release.artist_id == artist_id)
    if kind:
        q = q.where(Release.kind == kind)
    return (await session.scalars(q.order_by(Release.first_release_date.desc()))).all()


@router.get("/artist/{artist_id}/appearance", response_model=list[schemas.AppearanceOut])
async def artist_appearances(artist_id: int, session: AsyncSession = Depends(get_session)):
    a = await session.get(Artist, artist_id)
    if not a:
        raise HTTPException(404)
    return a.appearances


# --- releases / calendar ----------------------------------------------------------------


@router.get("/release", response_model=list[schemas.ReleaseOut])
async def list_releases(
    status: str | None = None,
    kind: str | None = None,
    monitored: bool | None = None,
    limit: int = 500,
    session: AsyncSession = Depends(get_session),
):
    q = select(Release)
    if status:
        q = q.where(Release.status == status)
    if kind:
        q = q.where(Release.kind == kind)
    if monitored is not None:
        q = q.where(Release.monitored.is_(monitored))
    return (await session.scalars(q.order_by(Release.first_release_date.desc()).limit(limit))).all()


@router.patch("/release/{release_id}", response_model=schemas.ReleaseOut)
async def patch_release(release_id: int, body: schemas.ReleasePatch, session: AsyncSession = Depends(get_session)):
    r = await session.get(Release, release_id)
    if not r:
        raise HTTPException(404)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(r, k, v)
    await session.commit()
    return r


@router.get("/calendar")
async def calendar(
    start: datetime | None = None,
    end: datetime | None = None,
    monitored_only: bool = True,
    unmonitored: bool = False,
    includeArtist: bool = True,
    session: AsyncSession = Depends(get_session),
):
    """Lidarr-shaped albums (plus Chordarr's fields) between two dates; Lidarr's parameters accepted."""
    start_d = (start.date() if start else date.today() - timedelta(days=30))
    end_d = (end.date() if end else date.today() + timedelta(days=90))
    q = select(Release).where(Release.first_release_date >= start_d, Release.first_release_date <= end_d)
    if monitored_only and not unmonitored:
        q = q.where(Release.monitored.is_(True))
    rows = (await session.scalars(q.order_by(Release.first_release_date))).all()
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    return [{**lidarr.album_payload(r, artists.get(r.artist_id), include_artist=includeArtist), **schemas.ReleaseOut.model_validate(r).model_dump(mode="json")} for r in rows]


@router.get("/wanted", response_model=list[schemas.ReleaseOut])
async def wanted(session: AsyncSession = Depends(get_session)):
    """Monitored releases that are already out but not in the library."""
    q = (
        select(Release)
        .where(Release.monitored.is_(True), Release.status == "missing", Release.first_release_date <= date.today())
        .order_by(Release.first_release_date.desc())
    )
    return (await session.scalars(q)).all()


# --- library ----------------------------------------------------------------------------


@router.get("/rootfolder")
async def rootfolder():
    if not settings.root_folder:
        return []
    import shutil

    root = settings.root_folder
    free = shutil.disk_usage(root).free if root.is_dir() else None
    return [{"id": 1, "path": str(root), "accessible": root.is_dir(), "freeSpace": free}]


@router.get("/library/unknown")
async def unknown_folders(session: AsyncSession = Depends(get_session)):
    known = {a.path for a in (await session.scalars(select(Artist))).all() if a.path}
    return [str(p) for p in library.unknown_artist_folders(known)]


# --- import -----------------------------------------------------------------------------


@router.post("/import/lidarr")
async def import_lidarr(body: schemas.LidarrImportIn, session: AsyncSession = Depends(get_session)):
    if body.db_path:
        rows = await asyncio.to_thread(lidarr_import.read_database, body.db_path)
    elif body.url and body.api_key:
        rows = await lidarr_import.read_api(body.url, body.api_key)
    else:
        raise HTTPException(400, "Give db_path, or url and api_key")
    added, skipped = [], []
    for row in rows:
        if body.only_monitored and not row.monitored:
            continue
        try:
            a = await artist_service.add_artist(session, row.mbid, path=row.path, monitored=row.monitored, monitor_types=["Album"], refresh=False)
            added.append(a.name)
            await session.commit()  # one short transaction per artist
        except artist_service.ArtistExists:
            skipped.append(row.name)
        except ValueError as exc:
            skipped.append(f"{row.name} ({exc})")
    scheduler.trigger("refresh")
    return {"added": added, "skipped": skipped}


# --- recommendations --------------------------------------------------------------------


@router.get("/recommendation", response_model=list[schemas.RecommendationOut])
async def list_recommendations(include_dismissed: bool = False, session: AsyncSession = Depends(get_session)):
    q = select(Recommendation)
    if not include_dismissed:
        q = q.where(Recommendation.dismissed.is_(False))
    return (await session.scalars(q.order_by(Recommendation.score.desc()))).all()


@router.post("/recommendation/{rec_id}/dismiss", response_model=schemas.RecommendationOut)
async def dismiss_recommendation(rec_id: int, session: AsyncSession = Depends(get_session)):
    r = await session.get(Recommendation, rec_id)
    if not r:
        raise HTTPException(404)
    r.dismissed = True
    await session.commit()
    return r


@router.post("/recommendation/{rec_id}/add")
async def add_recommendation(rec_id: int, session: AsyncSession = Depends(get_session)):
    r = await session.get(Recommendation, rec_id)
    if not r:
        raise HTTPException(404)
    mbid = r.mbid
    if not mbid:
        found = await mb.search_artists(r.name, limit=3)
        exact = [f for f in found if f.name.casefold() == r.name.casefold()]
        if not exact:
            raise HTTPException(404, f"No MusicBrainz artist named {r.name!r}")
        mbid = exact[0].mbid
    try:
        a = await artist_service.add_artist(session, mbid)
    except artist_service.ArtistExists as exc:
        raise HTTPException(409, f"{exc} is already tracked") from exc
    await session.delete(r)
    await session.commit()
    scheduler.wake()
    return artist_out(a)


# --- events / notifiers -----------------------------------------------------------------


@router.get("/event", response_model=list[schemas.EventOut])
async def list_events(limit: int = 100, type: str | None = None, session: AsyncSession = Depends(get_session)):
    q = select(Event)
    if type:
        q = q.where(Event.type == type)
    return (await session.scalars(q.order_by(Event.id.desc()).limit(limit))).all()


@router.get("/event/types")
async def event_types():
    return EVENT_TYPES


@router.get("/notification", response_model=list[schemas.NotifierOut])
async def list_notifiers(session: AsyncSession = Depends(get_session)):
    return (await session.scalars(select(Notifier).order_by(Notifier.id))).all()


@router.get("/notification/schema")
async def notifier_schema():
    return notify.NOTIFIER_TYPES


@router.post("/notification", response_model=schemas.NotifierOut, status_code=201)
async def create_notifier(body: schemas.NotifierIn, session: AsyncSession = Depends(get_session)):
    if body.type not in notify.NOTIFIER_TYPES:
        raise HTTPException(400, "unknown notifier type")
    n = Notifier(**body.model_dump())
    session.add(n)
    await session.commit()
    return n


@router.put("/notification/{notifier_id}", response_model=schemas.NotifierOut)
async def update_notifier(notifier_id: int, body: schemas.NotifierIn, session: AsyncSession = Depends(get_session)):
    n = await session.get(Notifier, notifier_id)
    if not n:
        raise HTTPException(404)
    for k, v in body.model_dump().items():
        setattr(n, k, v)
    await session.commit()
    return n


@router.delete("/notification/{notifier_id}", status_code=204)
async def delete_notifier(notifier_id: int, session: AsyncSession = Depends(get_session)):
    n = await session.get(Notifier, notifier_id)
    if n:
        await session.delete(n)
        await session.commit()


@router.post("/notification/{notifier_id}/test")
async def test_notifier(notifier_id: int, session: AsyncSession = Depends(get_session)):
    n = await session.get(Notifier, notifier_id)
    if not n:
        raise HTTPException(404)
    try:
        await notify.test(n)
    except Exception as exc:
        raise HTTPException(502, f"Notifier failed: {exc}") from exc
    return {"ok": True}


# --- stats ------------------------------------------------------------------------------


@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)):
    artists = await session.scalar(select(func.count(Artist.id)))
    own = await session.scalar(select(func.count(Release.id)).where(Release.kind == "own"))
    appearances = await session.scalar(select(func.count(Release.id)).where(Release.kind == "appearance"))
    have = await session.scalar(select(func.count(Release.id)).where(Release.status == "have"))
    wanted_n = await session.scalar(
        select(func.count(Release.id)).where(
            Release.monitored.is_(True), Release.status == "missing", Release.first_release_date <= date.today()
        )
    )
    upcoming = await session.scalar(
        select(func.count(Release.id)).where(Release.monitored.is_(True), Release.first_release_date > date.today())
    )
    return {"artists": artists, "releases": own, "appearances": appearances, "have": have, "wanted": wanted_n, "upcoming": upcoming}
