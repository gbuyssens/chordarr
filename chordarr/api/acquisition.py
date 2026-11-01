"""JSON API for acquisition: indexers (Lidarr-compatible), download clients, queue, media servers."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chordarr.api import lidarr_compat, schemas
from chordarr.api.auth import require_api_key
from chordarr.db import get_session
from chordarr.models import Artist, Download, DownloadClient, Indexer, MediaServer, Release, Tag
from chordarr.services import acquisition, downloadclients, importer, indexers, mediaservers, scheduler
from chordarr.services.prefs import QUALITY_PROFILES, get_prefs, set_prefs

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


def candidate_out(c: indexers.Candidate) -> dict:
    return {
        "title": c.title,
        "guid": c.guid,
        "downloadUrl": c.download_url,
        "indexerId": c.indexer_id,
        "indexer": c.indexer_name,
        "protocol": c.protocol,
        "size": c.size,
        "seeders": c.seeders,
        "leechers": c.leechers,
        "publishDate": c.published.isoformat() if c.published else None,
        "categories": c.categories,
        "infoUrl": c.info_url,
        "quality": c.quality,
        "score": c.score,
        "approved": c.accepted,
        "rejections": c.rejections,
    }


# --- indexers (Lidarr-shaped) -------------------------------------------------------------


@router.get("/indexer/schema")
async def indexer_schema():
    return lidarr_compat.schema()


@router.get("/indexer")
async def list_indexers(session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Indexer).order_by(Indexer.id))).all()
    return [lidarr_compat.to_resource(i) for i in rows]


async def _probe(indexer: Indexer) -> None:
    try:
        indexer.capabilities = await indexers.caps(indexer)
        indexer.last_error = None
    except Exception as exc:
        indexer.last_error = str(exc)[:500]
        log.warning("indexer %s: capabilities unavailable: %s", indexer.name, exc)


@router.post("/indexer", status_code=201)
async def create_indexer(body: dict = Body(...), forceSave: bool = False, session: AsyncSession = Depends(get_session)):
    indexer = Indexer(name="", base_url="")
    try:
        lidarr_compat.apply(indexer, body)
    except ValueError as exc:
        raise HTTPException(400, [{"propertyName": "", "errorMessage": str(exc)}]) from exc
    await _probe(indexer)
    session.add(indexer)
    await session.commit()
    return lidarr_compat.to_resource(indexer)


@router.post("/indexer/test")
async def test_indexer_resource(response: Response, body: dict = Body(default={})):
    """Prowlarr's connection test: validates the shape, answers with the API level header."""
    probe = Indexer(name="", base_url="")
    try:
        lidarr_compat.apply(probe, body)
    except ValueError as exc:
        raise HTTPException(400, [{"propertyName": "", "errorMessage": str(exc)}]) from exc
    response.headers["X-Application-Version"] = lidarr_compat.LIDARR_API_VERSION
    return {}


@router.get("/indexer/{indexer_id}")
async def get_indexer(indexer_id: int, session: AsyncSession = Depends(get_session)):
    i = await session.get(Indexer, indexer_id)
    if not i:
        raise HTTPException(404)
    return lidarr_compat.to_resource(i)


@router.put("/indexer/{indexer_id}")
async def update_indexer(indexer_id: int, body: dict = Body(...), forceSave: bool = False, session: AsyncSession = Depends(get_session)):
    i = await session.get(Indexer, indexer_id)
    if not i:
        raise HTTPException(404)
    try:
        lidarr_compat.apply(i, body)
    except ValueError as exc:
        raise HTTPException(400, [{"propertyName": "", "errorMessage": str(exc)}]) from exc
    await _probe(i)
    await session.commit()
    return lidarr_compat.to_resource(i)


@router.delete("/indexer/{indexer_id}")
async def delete_indexer(indexer_id: int, session: AsyncSession = Depends(get_session)):
    i = await session.get(Indexer, indexer_id)
    if i:
        await session.delete(i)
        await session.commit()
    return {}


@router.post("/indexer/{indexer_id}/test")
async def test_indexer(indexer_id: int, session: AsyncSession = Depends(get_session)):
    i = await session.get(Indexer, indexer_id)
    if not i:
        raise HTTPException(404)
    try:
        i.capabilities = await indexers.test(i)
        i.last_error = None
    except Exception as exc:
        i.last_error = str(exc)[:500]
        await session.commit()
        raise HTTPException(502, f"Indexer test failed: {exc}") from exc
    await session.commit()
    return {"ok": True, "capabilities": i.capabilities}


# --- tags -------------------------------------------------------------------------------


@router.get("/tag")
async def list_tags(session: AsyncSession = Depends(get_session)):
    return [{"id": t.id, "label": t.label} for t in (await session.scalars(select(Tag).order_by(Tag.id))).all()]


@router.post("/tag", status_code=201)
async def create_tag(body: dict = Body(...), session: AsyncSession = Depends(get_session)):
    label = (body.get("label") or "").strip().lower()
    if not label:
        raise HTTPException(400, "label is required")
    t = await session.scalar(select(Tag).where(Tag.label == label))
    if t is None:
        t = Tag(label=label)
        session.add(t)
        await session.commit()
    return {"id": t.id, "label": t.label}


@router.delete("/tag/{tag_id}")
async def delete_tag(tag_id: int, session: AsyncSession = Depends(get_session)):
    t = await session.get(Tag, tag_id)
    if t:
        await session.delete(t)
        await session.commit()
    return {}


# --- download clients -------------------------------------------------------------------


@router.get("/downloadclient/schema")
async def downloadclient_schema():
    return downloadclients.CLIENT_TYPES


@router.get("/downloadclient", response_model=list[schemas.DownloadClientOut])
async def list_downloadclients(session: AsyncSession = Depends(get_session)):
    return (await session.scalars(select(DownloadClient).order_by(DownloadClient.id))).all()


@router.post("/downloadclient", response_model=schemas.DownloadClientOut, status_code=201)
async def create_downloadclient(body: schemas.DownloadClientIn, session: AsyncSession = Depends(get_session)):
    if body.implementation not in downloadclients.CLIENT_TYPES:
        raise HTTPException(400, "unknown download client type")
    c = DownloadClient(**body.model_dump())
    session.add(c)
    await session.commit()
    return c


@router.put("/downloadclient/{client_id}", response_model=schemas.DownloadClientOut)
async def update_downloadclient(client_id: int, body: schemas.DownloadClientIn, session: AsyncSession = Depends(get_session)):
    c = await session.get(DownloadClient, client_id)
    if not c:
        raise HTTPException(404)
    for k, v in body.model_dump().items():
        setattr(c, k, v)
    await session.commit()
    return c


@router.delete("/downloadclient/{client_id}", status_code=204)
async def delete_downloadclient(client_id: int, session: AsyncSession = Depends(get_session)):
    c = await session.get(DownloadClient, client_id)
    if c:
        await session.delete(c)
        await session.commit()


@router.post("/downloadclient/{client_id}/test")
async def test_downloadclient(client_id: int, session: AsyncSession = Depends(get_session)):
    c = await session.get(DownloadClient, client_id)
    if not c:
        raise HTTPException(404)
    try:
        info = await downloadclients.test(c)
        c.last_error = None
    except Exception as exc:
        c.last_error = str(exc)[:500]
        await session.commit()
        raise HTTPException(502, f"Download client test failed: {exc}") from exc
    await session.commit()
    return {"ok": True, "info": info}


# --- media servers ----------------------------------------------------------------------


@router.get("/mediaserver/schema")
async def mediaserver_schema():
    return mediaservers.MEDIA_SERVER_TYPES


@router.get("/mediaserver", response_model=list[schemas.MediaServerOut])
async def list_mediaservers(session: AsyncSession = Depends(get_session)):
    return (await session.scalars(select(MediaServer).order_by(MediaServer.id))).all()


@router.post("/mediaserver", response_model=schemas.MediaServerOut, status_code=201)
async def create_mediaserver(body: schemas.MediaServerIn, session: AsyncSession = Depends(get_session)):
    if body.type not in mediaservers.MEDIA_SERVER_TYPES:
        raise HTTPException(400, "unknown media server type")
    m = MediaServer(**body.model_dump())
    session.add(m)
    await session.commit()
    return m


@router.put("/mediaserver/{server_id}", response_model=schemas.MediaServerOut)
async def update_mediaserver(server_id: int, body: schemas.MediaServerIn, session: AsyncSession = Depends(get_session)):
    m = await session.get(MediaServer, server_id)
    if not m:
        raise HTTPException(404)
    for k, v in body.model_dump().items():
        setattr(m, k, v)
    await session.commit()
    return m


@router.delete("/mediaserver/{server_id}", status_code=204)
async def delete_mediaserver(server_id: int, session: AsyncSession = Depends(get_session)):
    m = await session.get(MediaServer, server_id)
    if m:
        await session.delete(m)
        await session.commit()


@router.post("/mediaserver/{server_id}/test")
async def test_mediaserver(server_id: int, session: AsyncSession = Depends(get_session)):
    m = await session.get(MediaServer, server_id)
    if not m:
        raise HTTPException(404)
    try:
        info = await mediaservers.test(m)
        m.last_error = None
    except Exception as exc:
        m.last_error = str(exc)[:500]
        await session.commit()
        raise HTTPException(502, f"Media server test failed: {exc}") from exc
    await session.commit()
    return {"ok": True, "info": info}


@router.post("/mediaserver/{server_id}/scan")
async def scan_mediaserver(server_id: int, body: dict = Body(default={}), session: AsyncSession = Depends(get_session)):
    m = await session.get(MediaServer, server_id)
    if not m:
        raise HTTPException(404)
    path = body.get("path")
    if not path:
        raise HTTPException(400, "path is required")
    try:
        await mediaservers.notify(m, [path])
    except Exception as exc:
        raise HTTPException(502, f"Scan request failed: {exc}") from exc
    return {"ok": True}


# --- search / grab / queue --------------------------------------------------------------


@router.get("/release/{release_id}/search")
async def release_search(release_id: int, session: AsyncSession = Depends(get_session)):
    r = await session.get(Release, release_id)
    if not r:
        raise HTTPException(404)
    candidates = await acquisition.search_release(session, r)
    await session.commit()
    return [candidate_out(c) for c in candidates]


@router.post("/release/{release_id}/grab", response_model=schemas.DownloadOut)
async def release_grab(release_id: int, body: schemas.GrabIn, session: AsyncSession = Depends(get_session)):
    r = await session.get(Release, release_id)
    if not r:
        raise HTTPException(404)
    candidates = await acquisition.search_release(session, r)
    chosen = next((c for c in candidates if c.guid == body.guid and (body.indexer_id is None or c.indexer_id == body.indexer_id)), None)
    if chosen is None:
        raise HTTPException(404, "candidate not found in a fresh search; search again")
    try:
        dl = await acquisition.grab(session, r, chosen, manual=True)
    except acquisition.GrabError as exc:
        await session.commit()
        raise HTTPException(502, str(exc)) from exc
    await session.commit()
    return dl


@router.get("/queue", response_model=list[schemas.DownloadOut])
async def queue(include_finished: bool = Query(True), session: AsyncSession = Depends(get_session)):
    q = select(Download).order_by(Download.id.desc())
    if not include_finished:
        q = q.where(Download.status.in_(("grabbed", "downloading", "completed", "importing", "warning")))
    return (await session.scalars(q.limit(200))).all()


@router.delete("/queue/{download_id}", status_code=204)
async def queue_remove(download_id: int, delete_files: bool = False, session: AsyncSession = Depends(get_session)):
    d = await session.get(Download, download_id)
    if not d:
        return
    cfg = await session.get(DownloadClient, d.download_client_id) if d.download_client_id else None
    if cfg and d.client_id and d.status != "imported":
        try:
            async with downloadclients.make_client(cfg) as dc:
                await dc.remove(d.client_id, delete_files=delete_files)
        except Exception as exc:
            log.warning("remove %s from %s: %s", d.title, cfg.name, exc)
    await session.delete(d)
    await session.commit()


@router.post("/queue/{download_id}/import", response_model=schemas.DownloadOut)
async def queue_import(download_id: int, body: schemas.QueueImportIn | None = None, session: AsyncSession = Depends(get_session)):
    """Retry the import of a finished download, optionally forcing it or from another path."""
    d = await session.get(Download, download_id)
    if not d or not d.release:
        raise HTTPException(404)
    path = Path((body.path if body and body.path else None) or d.output_path or "")
    if not str(path):
        raise HTTPException(400, "no path known for this download; give one")
    try:
        result = await importer.import_folder(session, path, d.release, mode=importer.effective_mode(d.protocol), force=bool(body and body.force))
    except importer.ImportError_ as exc:
        d.status, d.message = "warning", f"import needs attention: {exc}"
        await session.commit()
        raise HTTPException(409, str(exc)) from exc
    d.status, d.progress, d.imported_at, d.output_path = "imported", 1.0, importer.utcnow(), str(path)
    d.message = f"{len(result.files)} files → {result.destination}"
    await session.commit()
    return d


@router.post("/import/manual")
async def import_manual(body: schemas.ManualImportIn, session: AsyncSession = Depends(get_session)):
    """Import a folder already on disk into a release (guessed from the tags when not given)."""
    path = Path(body.path)
    release = await session.get(Release, body.release_id) if body.release_id else await importer.guess_release(session, path)
    if release is None:
        raise HTTPException(404, "could not tell which release this folder holds; give release_id")
    artist = await session.get(Artist, release.artist_id)
    try:
        result = await importer.import_folder(session, path, release, artist, mode=body.mode or importer.effective_mode(None), force=body.force)
    except importer.ImportError_ as exc:
        raise HTTPException(409, str(exc)) from exc
    await session.commit()
    return {"release_id": release.id, "artist": artist.name, "title": release.title, "destination": str(result.destination), "files": len(result.files), "skipped": result.skipped, "warnings": result.warnings}


# --- preferences ------------------------------------------------------------------------


@router.get("/config/acquisition")
async def get_acquisition_config(session: AsyncSession = Depends(get_session)):
    p = await get_prefs(session)
    return {"auto_grab": p.auto_grab, "quality_profile": p.quality_profile, "quality_profiles": QUALITY_PROFILES}


@router.put("/config/acquisition")
async def put_acquisition_config(body: schemas.AcquisitionConfigIn, session: AsyncSession = Depends(get_session)):
    values = body.model_dump(exclude_none=True)
    if "quality_profile" in values and values["quality_profile"] not in QUALITY_PROFILES:
        raise HTTPException(400, "unknown quality profile")
    p = await set_prefs(session, **values)
    await session.commit()
    if p.auto_grab:
        scheduler.wake()
    return {"auto_grab": p.auto_grab, "quality_profile": p.quality_profile}


@router.get("/system/acquisition")
async def acquisition_status(request: Request, session: AsyncSession = Depends(get_session)):
    counts = {}
    for row in (await session.scalars(select(Download))).all():
        counts[row.status] = counts.get(row.status, 0) + 1
    return {"queue": counts, "jobs": scheduler.status(), "prefs": (await get_prefs(session)).__dict__}
