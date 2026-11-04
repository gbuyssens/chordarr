"""Server-rendered UI, laid out like the other *arr applications.

Sidebar: Artists (Add New, Library Import, Discover), Calendar, Activity (Queue, History),
Wanted (Missing), Settings (Media Management, Indexers, Download Clients, Connect, General),
System (Status, Tasks). Releases have an interactive Search page.
Jinja2 templates, plain forms, no build step, no CDN.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from chordarr import __version__
from chordarr.api import lidarr_compat
from chordarr.api.auth import COOKIE, session_token, ui_authenticated
from chordarr.config import settings
from chordarr.db import get_session
from chordarr.metadata import musicbrainz as mb
from chordarr.models import (
    Appearance,
    Artist,
    Download,
    DownloadClient,
    Event,
    ImportList,
    Indexer,
    MediaServer,
    Notifier,
    Recommendation,
    Release,
)
from chordarr.services import (
    acquisition,
    downloadclients,
    importer,
    indexers,
    library,
    lidarr_import,
    mediaservers,
    notify,
    scheduler,
)
from chordarr.services import artists as artist_service
from chordarr.services.events import EVENT_TYPES
from chordarr.services.indexers import human_size
from chordarr.services.prefs import QUALITY_PROFILES, get_prefs, save_overrides, set_prefs

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
templates.env.globals.update(
    version=__version__,
    settings=settings,
    EVENT_TYPES=EVENT_TYPES,
    NOTIFIER_TYPES=notify.NOTIFIER_TYPES,
    CLIENT_TYPES=downloadclients.CLIENT_TYPES,
    MEDIA_SERVER_TYPES=mediaservers.MEDIA_SERVER_TYPES,
    QUALITY_PROFILES=QUALITY_PROFILES,
    human_size=human_size,
    today=date.today,
    base=settings.url_base,
)

TYPE_ORDER = ["Album", "EP", "Single", "Broadcast", "Other"]


def url(path: str) -> str:
    return f"{settings.url_base}{path}"


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("notice", request.query_params.get("notice"))
    ctx.setdefault("jobs", scheduler.status())
    return templates.TemplateResponse(request, name, {"request": request, **ctx})


def back(request: Request, fallback: str) -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or url(fallback), status_code=303)


async def guard(request: Request):
    if not ui_authenticated(request):
        raise HTTPException(status_code=307, headers={"Location": url("/login")})


protected = APIRouter(dependencies=[Depends(guard)])


def artist_stats(a: Artist) -> dict:
    own = [r for r in a.releases if r.kind == "own"]
    monitored = [r for r in own if r.monitored]
    have = [r for r in monitored if r.status == "have"]
    missing = [r for r in monitored if r.status != "have" and r.first_release_date and r.first_release_date <= date.today()]
    feats = [r for r in a.releases if r.kind == "appearance"]
    return {
        "own": len(own),
        "monitored": len(monitored),
        "have": len(have),
        "missing": len(missing),
        "appearances": len(feats),
        "files": sum(r.file_count for r in a.releases),
        "pct": int(100 * len(have) / len(monitored)) if monitored else 0,
        "state": "complete" if monitored and not missing else ("missing" if missing else "none"),
    }


# --- login ------------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if ui_authenticated(request):
        return RedirectResponse(url("/"), status_code=303)
    return render(request, "login.html", error=None)


@router.post("/login")
async def login(request: Request, password: str = Form(...)):
    if settings.ui_password and hmac.compare_digest(password, settings.ui_password):
        resp = RedirectResponse(url("/"), status_code=303)
        resp.set_cookie(COOKIE, session_token(), httponly=True, samesite="lax", max_age=30 * 86400)
        return resp
    return render(request, "login.html", error="Wrong password")


@router.get("/logout")
async def logout():
    resp = RedirectResponse(url("/login"), status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# --- artists ----------------------------------------------------------------------------


@protected.get("/", response_class=HTMLResponse)
async def home():
    return RedirectResponse(url("/artists"), status_code=302)


@protected.get("/artists", response_class=HTMLResponse)
async def artists_index(request: Request, sort: str = "name", filter: str = "all", view: str = "posters", session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Artist))).all()
    items = [(a, artist_stats(a)) for a in rows]
    if filter == "monitored":
        items = [i for i in items if i[0].monitored]
    elif filter == "unmonitored":
        items = [i for i in items if not i[0].monitored]
    elif filter == "missing":
        items = [i for i in items if i[1]["missing"]]
    keys = {
        "name": lambda i: (i[0].sort_name or i[0].name).casefold(),
        "added": lambda i: i[0].added_at,
        "albums": lambda i: -i[1]["own"],
        "missing": lambda i: -i[1]["missing"],
    }
    items.sort(key=keys.get(sort, keys["name"]), reverse=sort == "added")
    return render(request, "artists/index.html", items=items, sort=sort, filter=filter, view=view, total=len(rows))


@protected.get("/artists/add", response_class=HTMLResponse)
async def add_artist_page(request: Request, q: str = "", session: AsyncSession = Depends(get_session)):
    results = await mb.search_artists(q) if q else []
    tracked = {a.mbid: a.id for a in (await session.scalars(select(Artist))).all()}
    return render(request, "artists/add.html", q=q, results=results, tracked=tracked)


@protected.post("/artists/add")
async def add_artist_submit(
    mbid: str = Form(...),
    monitor_album: bool = Form(False),
    monitor_ep: bool = Form(False),
    monitor_single: bool = Form(False),
    monitor_appearances: bool = Form(False),
    monitor: str = Form("all"),
    session: AsyncSession = Depends(get_session),
):
    types = [t for t, on in (("Album", monitor_album), ("EP", monitor_ep), ("Single", monitor_single)) if on]
    if monitor not in artist_service.MONITOR_MODES:
        monitor = "all"
    try:
        artist = await artist_service.add_artist(session, mbid, monitor_types=types or ["Album"], monitor_appearances=monitor_appearances, monitor=monitor)
    except artist_service.ArtistExists as exc:
        existing = await session.scalar(select(Artist).where(Artist.mbid == mbid))
        return RedirectResponse(url(f"/artists/{existing.id}?notice=Already+tracked:+{exc}"), status_code=303)
    await library.scan_artist(session, artist)
    await session.commit()
    scheduler.wake()
    return RedirectResponse(url(f"/artists/{artist.id}"), status_code=303)


@protected.get("/artists/import", response_class=HTMLResponse)
async def library_import_page(request: Request, session: AsyncSession = Depends(get_session)):
    known = {a.path for a in (await session.scalars(select(Artist))).all() if a.path}
    return render(request, "artists/import.html", unknown=library.unknown_artist_folders(known))


@protected.post("/artists/import/lidarr")
async def import_lidarr_ui(db_path: str = Form(""), lidarr_url: str = Form(""), api_key: str = Form(""), session: AsyncSession = Depends(get_session)):
    try:
        if db_path:
            rows = await asyncio.to_thread(lidarr_import.read_database, db_path)
        elif lidarr_url and api_key:
            rows = await lidarr_import.read_api(lidarr_url, api_key)
        else:
            raise ValueError("give a database path, or a URL and an API key")
    except Exception as exc:
        return RedirectResponse(url(f"/artists/import?notice=Import+failed:+{str(exc)[:100]}"), status_code=303)
    added = skipped = 0
    for row in rows:
        try:
            await artist_service.add_artist(session, row.mbid, path=row.path, monitored=row.monitored, monitor_types=["Album"], refresh=False)
            added += 1
            await session.commit()
        except (artist_service.ArtistExists, ValueError):
            skipped += 1
    scheduler.trigger("refresh")
    return RedirectResponse(url(f"/artists?notice={added}+artists+imported,+{skipped}+skipped.+Refresh+running."), status_code=303)


@protected.get("/artists/features", response_class=HTMLResponse)
async def features_page(request: Request, filter: str = "missing", session: AsyncSession = Depends(get_session)):
    """Every appearance (feature, remix, compilation slot) of every tracked artist, like a Wanted view."""
    q = select(Release).where(Release.kind == "appearance")
    if filter == "missing":
        q = q.where(Release.monitored.is_(True), Release.status.in_(("missing", "partial")), Release.first_release_date <= date.today())
    elif filter == "monitored":
        q = q.where(Release.monitored.is_(True))
    elif filter == "have":
        q = q.where(Release.status == "have")
    rows = (await session.scalars(q.order_by(Release.first_release_date.desc()).limit(1000))).all()
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    tracks: dict[int, list] = {}
    for ap in (await session.scalars(select(Appearance).where(Appearance.release_id.in_([r.id for r in rows] or [0])))).all():
        tracks.setdefault(ap.release_id, []).append(ap)
    counts = {
        "all": await session.scalar(select(func.count(Release.id)).where(Release.kind == "appearance")),
        "monitored": await session.scalar(select(func.count(Release.id)).where(Release.kind == "appearance", Release.monitored.is_(True))),
        "have": await session.scalar(select(func.count(Release.id)).where(Release.kind == "appearance", Release.status == "have")),
    }
    return render(request, "artists/features.html", releases=rows, artists=artists, tracks=tracks, filter=filter, counts=counts)


@protected.post("/artists/features/search-all")
async def features_search_all():
    scheduler.trigger_appearance_search()
    return RedirectResponse(url("/artists/features?notice=Searching+every+monitored+missing+appearance+in+the+background;+grabs+show+up+in+Activity+→+Queue"), status_code=303)


@protected.post("/artists/features/monitor-all")
async def features_monitor_all(monitored: str = Form("1"), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Release).where(Release.kind == "appearance"))).all()
    on = monitored == "1"
    for r in rows:
        roles = {ap.role for ap in r.appearances}
        if roles == {"compilation"}:
            continue  # compilation-only slots stay as they are
        r.monitored = on
    await session.commit()
    return RedirectResponse(url(f"/artists/features?notice={'Monitoring' if on else 'Unmonitoring'}+every+feature+and+remix+appearance"), status_code=303)


@protected.post("/artists/discover/add-top")
async def discover_add_top(count: int = Form(5), session: AsyncSession = Depends(get_session)):
    recs = (await session.scalars(select(Recommendation).where(Recommendation.dismissed.is_(False), Recommendation.mbid.is_not(None)).order_by(Recommendation.score.desc()).limit(max(1, min(count, 25))))).all()
    mbids = [r.mbid for r in recs]
    if not mbids:
        return RedirectResponse(url("/artists/discover?notice=No+recommendation+with+a+MusicBrainz+id+to+add"), status_code=303)
    scheduler.trigger_add_and_search(mbids)
    return RedirectResponse(url(f"/artists/discover?notice=Adding+{len(mbids)}+artists+and+grabbing+their+monitored+releases+in+the+background"), status_code=303)


@protected.get("/artists/discover", response_class=HTMLResponse)
async def discover_page(request: Request, session: AsyncSession = Depends(get_session)):
    recs = (await session.scalars(select(Recommendation).where(Recommendation.dismissed.is_(False)).order_by(Recommendation.score.desc()).limit(60))).all()
    return render(request, "artists/discover.html", recs=recs)


@protected.post("/artists/discover/{rec_id}/{action}")
async def discover_action(rec_id: int, action: str, session: AsyncSession = Depends(get_session)):
    r = await session.get(Recommendation, rec_id)
    if not r:
        raise HTTPException(404)
    if action == "dismiss":
        r.dismissed = True
        await session.commit()
        return RedirectResponse(url("/artists/discover"), status_code=303)
    if action == "add-search":
        if not r.mbid:
            found = await mb.search_artists(r.name, limit=5)
            exact = [f for f in found if f.name.casefold() == r.name.casefold()]
            if not exact:
                return RedirectResponse(url(f"/artists/add?q={r.name}"), status_code=303)
            r.mbid = exact[0].mbid
            await session.commit()
        scheduler.trigger_add_and_search([r.mbid])
        return RedirectResponse(url(f"/artists/discover?notice=Adding+{r.name}+and+grabbing+its+monitored+releases+in+the+background"), status_code=303)
    mbid = r.mbid
    if not mbid:
        found = await mb.search_artists(r.name, limit=5)
        exact = [f for f in found if f.name.casefold() == r.name.casefold()]
        if not exact:
            return RedirectResponse(url(f"/artists/add?q={r.name}"), status_code=303)
        mbid = exact[0].mbid
    try:
        a = await artist_service.add_artist(session, mbid)
    except artist_service.ArtistExists:
        a = await session.scalar(select(Artist).where(Artist.mbid == mbid))
    await session.delete(r)
    await session.commit()
    scheduler.wake()
    return RedirectResponse(url(f"/artists/{a.id}"), status_code=303)


@protected.get("/artists/{artist_id}", response_class=HTMLResponse)
async def artist_page(request: Request, artist_id: int, session: AsyncSession = Depends(get_session)):
    a = await session.get(Artist, artist_id)
    if not a:
        raise HTTPException(404)
    own = sorted((r for r in a.releases if r.kind == "own"), key=lambda r: (r.first_release_date or date.min), reverse=True)
    groups: OrderedDict[str, list[Release]] = OrderedDict()
    for t in TYPE_ORDER:
        rels = [r for r in own if r.primary_type == t]
        if rels:
            groups[t] = rels
    for r in own:
        if r.primary_type not in TYPE_ORDER:
            groups.setdefault(r.primary_type, []).append(r)
    appearances = sorted((r for r in a.releases if r.kind == "appearance"), key=lambda r: (r.first_release_date or date.min), reverse=True)
    tracks: dict[int, list] = {}
    for ap in a.appearances:
        tracks.setdefault(ap.release_id, []).append(ap)
    events = (await session.scalars(select(Event).where(Event.artist_id == a.id).order_by(Event.id.desc()).limit(8))).all()
    return render(request, "artists/detail.html", artist=a, stats=artist_stats(a), groups=groups, appearances=appearances, tracks=tracks, folder=library.artist_folder(a), events=events)


@protected.post("/artists/{artist_id}/toggle")
async def artist_toggle(request: Request, artist_id: int, field: str = Form(...), session: AsyncSession = Depends(get_session)):
    a = await session.get(Artist, artist_id)
    if not a:
        raise HTTPException(404)
    if field == "monitored":
        a.monitored = not a.monitored
    elif field == "monitor_appearances":
        a.monitor_appearances = not a.monitor_appearances
    elif field in TYPE_ORDER:
        types = list(a.monitor_types or [])
        a.monitor_types = [t for t in types if t != field] if field in types else types + [field]
    await session.commit()
    return back(request, f"/artists/{artist_id}")


@protected.post("/artists/{artist_id}/refresh")
async def artist_refresh(artist_id: int, session: AsyncSession = Depends(get_session)):
    a = await session.get(Artist, artist_id)
    if not a:
        raise HTTPException(404)
    await artist_service.refresh_artist(session, a, announce=True)
    await session.refresh(a, ["releases"])
    await library.scan_artist(session, a)
    await session.commit()
    scheduler.trigger("notify")
    return RedirectResponse(url(f"/artists/{artist_id}?notice=Refreshed+and+scanned"), status_code=303)


@protected.post("/artists/{artist_id}/search")
async def artist_search_monitored(artist_id: int, session: AsyncSession = Depends(get_session)):
    """Lidarr's "Search Monitored": every monitored, missing, released item of the artist."""
    a = await session.get(Artist, artist_id)
    if not a:
        raise HTTPException(404)
    n = len(await acquisition.monitored_missing(session, artist_id))
    if n == 0:
        return RedirectResponse(url(f"/artists/{artist_id}?notice=Nothing+to+search:+every+monitored+release+is+on+disk+or+already+queued"), status_code=303)
    scheduler.trigger_artist_search(artist_id)
    return RedirectResponse(url(f"/artists/{artist_id}?notice=Searching+{n}+monitored+release{'s' if n > 1 else ''}+in+the+background;+grabs+show+up+in+Activity+→+Queue"), status_code=303)


@protected.post("/artists/{artist_id}/delete")
async def artist_delete(artist_id: int, session: AsyncSession = Depends(get_session)):
    a = await session.get(Artist, artist_id)
    if a:
        await session.delete(a)
        await session.commit()
    return RedirectResponse(url("/artists?notice=Artist+removed,+files+kept"), status_code=303)


@protected.post("/releases/{release_id}/toggle")
async def release_toggle(request: Request, release_id: int, session: AsyncSession = Depends(get_session)):
    r = await session.get(Release, release_id)
    if not r:
        raise HTTPException(404)
    r.monitored = not r.monitored
    await session.commit()
    return back(request, f"/artists/{r.artist_id}")


# --- calendar ---------------------------------------------------------------------------


@protected.get("/calendar", response_class=HTMLResponse)
async def calendar_page(request: Request, session: AsyncSession = Depends(get_session)):
    today = date.today()
    rows = (
        await session.scalars(
            select(Release)
            .where(Release.monitored.is_(True), Release.first_release_date >= today - timedelta(days=60), Release.first_release_date <= today + timedelta(days=365))
            .order_by(Release.first_release_date.desc())
        )
    ).all()
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    days: OrderedDict[date, list[Release]] = OrderedDict()
    for r in rows:
        days.setdefault(r.first_release_date, []).append(r)
    return render(request, "calendar.html", days=days, artists=artists)


# --- activity ---------------------------------------------------------------------------


@protected.get("/activity")
async def activity_root():
    return RedirectResponse(url("/activity/history"), status_code=302)


@protected.get("/activity/queue", response_class=HTMLResponse)
async def queue_page(request: Request, finished: int = 0, session: AsyncSession = Depends(get_session)):
    q = select(Download).order_by(Download.id.desc()).limit(200)
    if not finished:
        q = q.where(Download.status != "imported")
    downloads = (await session.scalars(q)).all()
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    wanted = (
        await session.scalars(
            select(Release).where(Release.status.in_(("missing", "partial")), Release.first_release_date <= date.today()).order_by(Release.first_release_date.desc()).limit(400)
        )
    ).all()
    wanted.sort(key=lambda r: (artists[r.artist_id].sort_name if r.artist_id in artists else "", r.title))
    clients = (await session.scalars(select(DownloadClient).where(DownloadClient.enabled.is_(True)))).all()
    indexer_names = {i.id: i.name for i in (await session.scalars(select(Indexer))).all()}
    client_names = {c.id: c.name for c in (await session.scalars(select(DownloadClient))).all()}
    return render(request, "activity/queue.html", downloads=downloads, artists=artists, wanted=wanted, clients=clients, prefs=await get_prefs(session), show_finished=bool(finished), indexer_names=indexer_names, client_names=client_names)


@protected.post("/activity/queue/search-all")
async def queue_search_all():
    scheduler.trigger("search")
    return RedirectResponse(url("/activity/queue?notice=Searching+every+missing+release;+the+best+result+of+each+is+grabbed"), status_code=303)


@protected.post("/activity/queue/refresh")
async def queue_refresh():
    scheduler.trigger("queue")
    return RedirectResponse(url("/activity/queue?notice=Checking+downloads"), status_code=303)


@protected.post("/activity/queue/import")
async def manual_import_ui(path: str = Form(...), release_id: str = Form(""), mode: str = Form(""), force: bool = Form(False), session: AsyncSession = Depends(get_session)):
    src = Path(path.strip())
    release = await session.get(Release, int(release_id)) if release_id else await importer.guess_release(session, src)
    if release is None:
        return RedirectResponse(url("/activity/queue?notice=Could+not+tell+which+release+this+folder+holds;+pick+one#manual"), status_code=303)
    try:
        result = await importer.import_folder(session, src, release, mode=mode or importer.effective_mode(None), force=force)
    except importer.ImportError_ as exc:
        return RedirectResponse(url(f"/activity/queue?notice=Import+failed:+{str(exc)[:140]}#manual"), status_code=303)
    await session.commit()
    return RedirectResponse(url(f"/artists/{release.artist_id}?notice=Imported+{len(result.files)}+files+into+{result.destination.name}"), status_code=303)


@protected.post("/activity/queue/{download_id}/remove")
async def queue_remove(request: Request, download_id: int, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    d = await session.get(Download, download_id)
    if not d:
        raise HTTPException(404)
    remove_from_client = _form_bool(form, "remove_from_client")
    delete_files = _form_bool(form, "delete_files") or (d.protocol == "direct" and remove_from_client)
    cfg = await session.get(DownloadClient, d.download_client_id) if d.download_client_id else None
    if remove_from_client and cfg and d.client_id and d.status != "imported":
        try:
            async with downloadclients.make_client(cfg) as dc:
                await dc.remove(d.client_id, delete_files=delete_files)
        except Exception as exc:
            log.warning("remove %s from %s: %s", d.title, cfg.name, exc)
    await session.delete(d)
    await session.commit()
    return RedirectResponse(url("/activity/queue?notice=Removed+from+the+queue"), status_code=303)


@protected.post("/activity/queue/{download_id}/retry")
async def queue_retry_ui(download_id: int, session: AsyncSession = Depends(get_session)):
    from chordarr.services import direct

    d = await session.get(Download, download_id)
    if not d or d.protocol != "direct":
        raise HTTPException(404)
    d.status, d.message, d.progress, d.client_id = "grabbed", "retry requested", 0.0, None
    await session.flush()
    await direct.resume(session)
    await session.commit()
    return RedirectResponse(url("/activity/queue?notice=Download+restarted"), status_code=303)


@protected.post("/activity/queue/{download_id}/import")
async def queue_import_ui(download_id: int, force: bool = Form(False), session: AsyncSession = Depends(get_session)):
    d = await session.get(Download, download_id)
    if not d or not d.release or not d.output_path:
        return RedirectResponse(url("/activity/queue?notice=Nothing+to+import+for+this+entry"), status_code=303)
    try:
        result = await importer.import_folder(session, Path(d.output_path), d.release, mode=importer.effective_mode(d.protocol), force=force)
    except importer.ImportError_ as exc:
        d.status, d.message = "warning", f"import needs attention: {exc}"
        await session.commit()
        return RedirectResponse(url(f"/activity/queue?notice=Import+failed:+{str(exc)[:120]}"), status_code=303)
    d.status, d.progress, d.imported_at = "imported", 1.0, importer.utcnow()
    d.message = f"{len(result.files)} files → {result.destination}"
    await session.commit()
    return RedirectResponse(url(f"/activity/queue?notice=Imported+{len(result.files)}+files"), status_code=303)


@protected.get("/activity/history", response_class=HTMLResponse)
async def history_page(request: Request, type: str = "", session: AsyncSession = Depends(get_session)):
    q = select(Event).order_by(Event.id.desc()).limit(300)
    if type:
        q = q.where(Event.type == type)
    events = (await session.scalars(q)).all()
    return render(request, "activity/history.html", events=events, type=type)


# --- wanted -----------------------------------------------------------------------------


@protected.get("/wanted")
async def wanted_root():
    return RedirectResponse(url("/wanted/missing"), status_code=302)


@protected.get("/wanted/missing", response_class=HTMLResponse)
async def missing_page(request: Request, kind: str = "", session: AsyncSession = Depends(get_session)):
    q = select(Release).where(Release.monitored.is_(True), Release.status.in_(("missing", "partial")), Release.first_release_date <= date.today())
    if kind:
        q = q.where(Release.kind == kind)
    rows = (await session.scalars(q.order_by(Release.first_release_date.desc()))).all()
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    return render(request, "wanted/missing.html", releases=rows, artists=artists, kind=kind)


# --- settings ---------------------------------------------------------------------------


# --- system -----------------------------------------------------------------------------


@protected.get("/system")
async def system_root():
    return RedirectResponse(url("/system/status"), status_code=302)


@protected.get("/system/status", response_class=HTMLResponse)
async def system_status(request: Request, session: AsyncSession = Depends(get_session)):
    import platform

    issues = []
    if not settings.root_folder:
        issues.append(("warning", "No root folder configured (CHORDARR_ROOT_FOLDER)."))
    for folder in library.unwritable_folders():
        issues.append(("error", f"Folder not writable by Chordarr: {folder}. Imports into it fail; fix the owner or give the group write access (chmod g+w)."))
    for a in (await session.scalars(select(Artist).where(Artist.refresh_error.is_not(None)))).all():
        issues.append(("error", f"{a.name}: {a.refresh_error}"))
    for name, err in scheduler.status()["errors"].items():
        issues.append(("error", f"Task {name}: {err}"))
    counts = {
        "artists": await session.scalar(select(func.count(Artist.id))),
        "releases": await session.scalar(select(func.count(Release.id)).where(Release.kind == "own")),
        "appearances": await session.scalar(select(func.count(Release.id)).where(Release.kind == "appearance")),
        "have": await session.scalar(select(func.count(Release.id)).where(Release.status == "have")),
    }
    about = {
        "Version": __version__,
        "Python": platform.python_version(),
        "OS": f"{platform.system()} {platform.release()}",
        "Data directory": str(settings.data_dir),
        "Database": str(settings.db_path),
        "Startup": "systemd / python -m chordarr",
    }
    return render(request, "system/status.html", issues=issues, counts=counts, about=about)


@protected.get("/system/tasks", response_class=HTMLResponse)
async def system_tasks(request: Request):
    tasks = [
        ("refresh", "Refresh artists", f"every {settings.refresh_interval_hours:g} h", "MusicBrainz catalogue and appearances of every monitored artist"),
        ("scan", "Scan library", f"every {settings.scan_interval_hours:g} h", "match folders and tags to releases"),
        ("recommendations", "Rebuild recommendations", f"every {settings.recommendations_interval_hours:g} h", "ListenBrainz and Deezer neighbours of tracked artists"),
        ("notify", "Send notifications", "every minute", "dispatch pending events to connections"),
        ("rss", "RSS sync", f"every {settings.rss_interval_minutes:g} min", "read indexer feeds and grab missing releases that appear (when automatic grabbing is on)"),
        ("search", "Search missing", f"every {settings.search_interval_hours:g} h", "search indexers for missing releases (when automatic grabbing is on)"),
        ("queue", "Check downloads", f"every {settings.queue_poll_seconds:g} s", "poll download clients and import finished downloads"),
    ]
    return render(request, "system/tasks.html", tasks=tasks)


@protected.post("/system/tasks/{name}")
async def run_task(name: str):
    if name not in ("refresh", "scan", "recommendations", "notify", "rss", "search", "queue"):
        raise HTTPException(404)
    scheduler.trigger(name)
    return RedirectResponse(url(f"/system/tasks?notice={name}+started"), status_code=303)


# --- settings: indexers, download clients -----------------------------------------------


# --- search and grab --------------------------------------------------------------------


@protected.get("/releases/{release_id}/search", response_class=HTMLResponse)
async def release_search_page(request: Request, release_id: int, session: AsyncSession = Depends(get_session)):
    r = await session.get(Release, release_id)
    if not r:
        raise HTTPException(404)
    artist = await session.get(Artist, r.artist_id)
    candidates = await acquisition.search_release(session, r)
    idx = (await session.scalars(select(Indexer).order_by(Indexer.priority))).all()
    prefs = await get_prefs(session)
    await session.commit()
    return render(request, "releases/search.html", release=r, artist=artist, candidates=candidates, indexers=[i for i in idx if i.enable_interactive_search], prefs=prefs)


@protected.post("/releases/{release_id}/search/auto")
async def release_search_auto(release_id: int, session: AsyncSession = Depends(get_session)):
    """Lidarr's automatic search: search everything and grab the best accepted result."""
    r = await session.get(Release, release_id)
    if not r:
        raise HTTPException(404)
    try:
        dl = await acquisition.grab_best(session, r)
    except acquisition.GrabError as exc:
        await session.commit()
        return RedirectResponse(url(f"/releases/{release_id}/search?notice=Grab+failed:+{str(exc)[:120]}"), status_code=303)
    await session.commit()
    if dl is None:
        return RedirectResponse(url(f"/releases/{release_id}/search?notice=No+acceptable+result+for+an+automatic+grab;+pick+one+by+hand+or+adjust+the+profile"), status_code=303)
    scheduler.wake()
    return RedirectResponse(url(f"/activity/queue?notice={dl.title[:60]}+sent+to+the+download+client"), status_code=303)


@protected.post("/releases/{release_id}/grab")
async def release_grab_submit(release_id: int, guid: str = Form(...), indexer_id: int = Form(...), session: AsyncSession = Depends(get_session)):
    r = await session.get(Release, release_id)
    if not r:
        raise HTTPException(404)
    candidates = await acquisition.search_release(session, r)
    chosen = next((c for c in candidates if c.guid == guid and c.indexer_id == indexer_id), None)
    if chosen is None:
        return RedirectResponse(url(f"/releases/{release_id}/search?notice=That+result+is+gone,+search+again"), status_code=303)
    try:
        await acquisition.grab(session, r, chosen, manual=True)
    except acquisition.GrabError as exc:
        await session.commit()
        return RedirectResponse(url(f"/releases/{release_id}/search?notice=Grab+failed:+{str(exc)[:120]}"), status_code=303)
    await session.commit()
    scheduler.wake()
    return RedirectResponse(url("/activity/queue?notice=Sent+to+the+download+client"), status_code=303)
@protected.get("/system/backup", response_class=HTMLResponse)
async def system_backup(request: Request):
    from chordarr.services import backup

    return render(request, "system/backup.html", backups=backup.list_backups())


@protected.post("/system/backup")
async def system_backup_now():
    from chordarr.services import backup

    path = await backup.create("manual")
    return RedirectResponse(url(f"/system/backup?notice=Backup+written:+{path.name}"), status_code=303)


@protected.post("/system/backup/delete")
async def system_backup_delete(kind: str = Form(...), name: str = Form(...)):
    from chordarr.services import backup

    p = backup.find(kind, name)
    if p:
        p.unlink()
    return RedirectResponse(url("/system/backup?notice=Backup+deleted"), status_code=303)


@protected.get("/backup/{kind}/{name}")
async def backup_download(kind: str, name: str):
    from fastapi.responses import FileResponse

    from chordarr.services import backup

    p = backup.find(kind, name)
    if not p:
        raise HTTPException(404)
    return FileResponse(p, filename=name, media_type="application/zip")


@protected.get("/system/updates", response_class=HTMLResponse)
async def system_updates(request: Request):
    from chordarr.services import updates

    releases = await updates.lidarr_shape()
    latest = updates.latest_version(await updates.check())
    return render(request, "system/updates.html", releases=releases, latest=latest, newer=updates.is_newer(latest))


@protected.post("/system/updates/check")
async def system_updates_check():
    from chordarr.services import updates

    await updates.check(force=True)
    return RedirectResponse(url("/system/updates"), status_code=303)


# --- settings (laid out like Lidarr: index, FieldSets, provider cards, edit pages) ------------


def _form_bool(form, name: str) -> bool:
    return str(form.get(name, "")).lower() in ("1", "true", "on", "yes")


@protected.get("/settings", response_class=HTMLResponse)
async def settings_index(request: Request):
    return render(request, "settings/index.html")


@protected.get("/settings/mediamanagement", response_class=HTMLResponse)
async def settings_media(request: Request, session: AsyncSession = Depends(get_session)):
    import shutil

    root = settings.root_folder
    free = shutil.disk_usage(root).free if root and root.is_dir() else None
    known = {a.path for a in (await session.scalars(select(Artist))).all() if a.path}
    unmapped = len(library.unknown_artist_folders(known))
    return render(request, "settings/mediamanagement.html", root=root, free=free, unmapped=unmapped)


@protected.post("/settings/mediamanagement")
async def settings_media_save(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    mode = str(form.get("import_mode") or "auto")
    await save_overrides(
        session,
        artist_folder_format=str(form.get("artist_folder_format") or "{artist}"),
        album_folder_format=str(form.get("album_folder_format") or "{album} ({year})"),
        track_format=str(form.get("track_format") or "{nn} - {title}"),
        import_mode=mode if mode in ("auto", "hardlink", "copy", "move") else "auto",
    )
    await session.commit()
    return RedirectResponse(url("/settings/mediamanagement?notice=Settings+saved"), status_code=303)


@protected.get("/settings/profiles", response_class=HTMLResponse)
async def settings_profiles(request: Request, session: AsyncSession = Depends(get_session)):
    return render(request, "settings/profiles.html", prefs=await get_prefs(session), QUALITY_PROFILES=QUALITY_PROFILES)


@protected.post("/settings/profiles")
async def settings_profiles_save(quality_profile: str = Form("lossless"), session: AsyncSession = Depends(get_session)):
    if quality_profile not in QUALITY_PROFILES:
        raise HTTPException(400)
    await set_prefs(session, quality_profile=quality_profile)
    await session.commit()
    return RedirectResponse(url("/settings/profiles?notice=Profile+saved"), status_code=303)


# indexers


@protected.get("/settings/indexers", response_class=HTMLResponse)
async def settings_indexers(request: Request, session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Indexer).order_by(Indexer.priority, Indexer.id))).all()
    return render(request, "settings/indexers.html", indexers=rows, prefs=await get_prefs(session))


@protected.post("/settings/indexers/options")
async def indexer_options(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    await set_prefs(session, auto_grab=_form_bool(form, "auto_grab"))
    await save_overrides(session, rss_interval_minutes=form.get("rss_interval_minutes") or 30, search_interval_hours=form.get("search_interval_hours") or 6)
    await session.commit()
    scheduler.wake()
    return RedirectResponse(url("/settings/indexers?notice=Settings+saved"), status_code=303)


@protected.get("/settings/indexers/add", response_class=HTMLResponse)
async def indexer_add(request: Request):
    presets = [
        ("Torznab", "Torznab", "Torrent indexer speaking the Torznab API (Jackett, Prowlarr, Gazelle gateways)."),
        ("Newznab", "Newznab", "Usenet indexer speaking the Newznab API."),
    ]
    return render(request, "settings/provider_add.html", kind="indexer", title="Add Indexer", presets=presets, new_url=url("/settings/indexers/new"), hint="The easy way: add Chordarr to Prowlarr as a Lidarr application. Prowlarr pushes its indexers here and keeps them in sync.")


@protected.get("/settings/indexers/new", response_class=HTMLResponse)
async def indexer_new(request: Request, implementation: str = "Torznab"):
    i = Indexer(name="", implementation="Newznab" if implementation == "Newznab" else "Torznab", base_url="", api_path="/api", api_key="", categories=[3000, 3010, 3040], priority=25, minimum_seeders=1)
    return render(request, "settings/indexer_edit.html", i=i, is_new=True)


@protected.get("/settings/indexers/{indexer_id}", response_class=HTMLResponse)
async def indexer_edit(request: Request, indexer_id: int, session: AsyncSession = Depends(get_session)):
    i = await session.get(Indexer, indexer_id)
    if not i:
        raise HTTPException(404)
    return render(request, "settings/indexer_edit.html", i=i, is_new=False)


@protected.post("/settings/indexers/save")
async def indexer_save(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    iid = form.get("id")
    i = await session.get(Indexer, int(iid)) if iid else Indexer(name="", base_url="")
    if i is None:
        raise HTTPException(404)
    try:
        cats = [int(c) for c in str(form.get("categories", "3000")).replace(";", ",").split(",") if c.strip()]
        i.name = str(form.get("name") or i.name or "Indexer")
        i.implementation = "Newznab" if form.get("implementation") == "Newznab" else "Torznab"
        i.enable_rss = _form_bool(form, "enable_rss")
        i.enable_automatic_search = _form_bool(form, "enable_automatic_search")
        i.enable_interactive_search = _form_bool(form, "enable_interactive_search")
        i.base_url = str(form.get("base_url", "")).strip()
        i.api_path = str(form.get("api_path") or "/api")
        if form.get("api_key") not in (None, "", lidarr_compat.MASK):
            i.api_key = str(form.get("api_key"))
        i.categories = cats or [3000]
        i.priority = int(form.get("priority") or 25)
        i.minimum_seeders = int(form.get("minimum_seeders") or 0)
        i.seed_ratio = float(form.get("seed_ratio")) if form.get("seed_ratio") else None
        i.seed_time = int(form.get("seed_time")) if form.get("seed_time") else None
        i.early_release_limit = int(form.get("early_release_limit")) if form.get("early_release_limit") else None
        i.additional_parameters = str(form.get("additional_parameters") or "")
        dc = form.get("download_client_id")
        i.download_client_id = int(dc) if dc else None
    except ValueError as exc:
        return RedirectResponse(url(f"/settings/indexers?notice=Invalid+value:+{str(exc)[:80]}"), status_code=303)
    if not i.base_url:
        return RedirectResponse(url("/settings/indexers?notice=URL+is+required"), status_code=303)
    try:
        i.capabilities = await indexers.caps(i)
        i.last_error = None
    except Exception as exc:
        i.last_error = str(exc)[:300]
    session.add(i)
    await session.commit()
    return RedirectResponse(url("/settings/indexers?notice=" + ("Indexer+saved" if not i.last_error else "Indexer+saved,+but+unreachable")), status_code=303)


@protected.post("/settings/indexers/{indexer_id}/{action}")
async def indexer_action(indexer_id: int, action: str, session: AsyncSession = Depends(get_session)):
    i = await session.get(Indexer, indexer_id)
    if not i:
        raise HTTPException(404)
    notice, target = "", "/settings/indexers"
    if action == "delete":
        await session.delete(i)
        notice = "Indexer+deleted"
    elif action == "test":
        target = f"/settings/indexers/{indexer_id}"
        try:
            i.capabilities = await indexers.test(i)
            i.last_error = None
            notice = f"Test+succeeded:+music+search+params+{','.join(i.capabilities.get('music_params') or ['none'])}"
        except Exception as exc:
            i.last_error = str(exc)[:300]
            notice = f"Test+failed:+{str(exc)[:80]}"
    await session.commit()
    return RedirectResponse(url(f"{target}?notice={notice}"), status_code=303)


# download clients


@protected.get("/settings/downloadclients", response_class=HTMLResponse)
async def settings_downloadclients(request: Request, session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(DownloadClient).order_by(DownloadClient.id))).all()
    return render(request, "settings/downloadclients.html", clients=rows)


@protected.post("/settings/downloadclients/options")
async def downloadclient_options(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    await save_overrides(session, queue_poll_seconds=form.get("queue_poll_seconds") or 60, import_mode=form.get("import_mode") or "auto")
    await session.commit()
    return RedirectResponse(url("/settings/downloadclients?notice=Settings+saved"), status_code=303)


@protected.get("/settings/downloadclients/add", response_class=HTMLResponse)
async def downloadclient_add(request: Request):
    descriptions = {"torrent": "Torrent client", "usenet": "Usenet client", "direct": "Direct source: downloads albums from Qobuz, Deezer or Tidal with your own accounts (streamrip)"}
    presets = [(k, d["label"], descriptions.get(d["protocol"], d["protocol"])) for k, d in downloadclients.CLIENT_TYPES.items()]
    return render(request, "settings/provider_add.html", kind="downloadclient", title="Add Download Client", presets=presets, new_url=url("/settings/downloadclients/new"), hint="")


@protected.get("/settings/downloadclients/new", response_class=HTMLResponse)
async def downloadclient_new(request: Request, implementation: str = "qbittorrent"):
    if implementation not in downloadclients.CLIENT_TYPES:
        raise HTTPException(404)
    from chordarr.services import direct

    c = DownloadClient(name=downloadclients.CLIENT_TYPES[implementation]["label"], implementation=implementation, url="http://localhost:8080" if implementation != "streamrip" else "streamrip://local", category="chordarr", settings={})
    return render(request, "settings/downloadclient_edit.html", c=c, is_new=True, parts=lidarr_compat.split_url(c.url), streamrip_available=direct.available())


@protected.get("/settings/downloadclients/{client_id}", response_class=HTMLResponse)
async def downloadclient_edit(request: Request, client_id: int, session: AsyncSession = Depends(get_session)):
    from chordarr.services import direct

    c = await session.get(DownloadClient, client_id)
    if not c:
        raise HTTPException(404)
    return render(request, "settings/downloadclient_edit.html", c=c, is_new=False, parts=lidarr_compat.split_url(c.url), streamrip_available=direct.available())


@protected.post("/settings/downloadclients/save")
async def downloadclient_save(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    cid = form.get("id")
    c = await session.get(DownloadClient, int(cid)) if cid else DownloadClient(name="", implementation=str(form.get("implementation") or "qbittorrent"), url="")
    if c is None:
        raise HTTPException(404)
    if c.implementation not in downloadclients.CLIENT_TYPES:
        raise HTTPException(400)
    c.name = str(form.get("name") or c.name)
    c.enabled = _form_bool(form, "enabled")
    c.priority = int(form.get("priority") or 1)
    if c.implementation == "streamrip":
        from chordarr.services import direct

        st = dict(c.settings or {})
        for key in direct.FIELDS:
            v = form.get(key)
            if v is None or v == lidarr_compat.MASK:
                continue
            v = str(v).strip()
            if key == "qobuz_password" and v:
                v = direct.md5_password(v)
            if v or key in st:
                st[key] = v
        c.settings = st
        c.url = c.url or "streamrip://local"
        c.category = "streamrip"
        session.add(c)
        await session.commit()
        direct.forget(c.id)
        return RedirectResponse(url("/settings/downloadclients?notice=Download+client+saved"), status_code=303)
    host = str(form.get("host", "")).strip()
    if not host:
        return RedirectResponse(url("/settings/downloadclients?notice=Host+is+required"), status_code=303)
    c.url = lidarr_compat.join_url(host, form.get("port") or 8080, _form_bool(form, "use_ssl"), str(form.get("url_base") or ""))
    c.username = str(form.get("username") or "")
    if form.get("password") not in (None, "", lidarr_compat.MASK):
        c.password = str(form.get("password"))
    if form.get("api_key") not in (None, "", lidarr_compat.MASK):
        c.api_key = str(form.get("api_key"))
    c.category = str(form.get("category") or "chordarr")
    c.remove_completed = _form_bool(form, "remove_completed")
    c.remote_path = str(form.get("remote_path") or "").strip()
    c.local_path = str(form.get("local_path") or "").strip()
    session.add(c)
    await session.commit()
    return RedirectResponse(url("/settings/downloadclients?notice=Download+client+saved"), status_code=303)


@protected.post("/settings/downloadclients/{client_id}/{action}")
async def downloadclient_action(client_id: int, action: str, session: AsyncSession = Depends(get_session)):
    c = await session.get(DownloadClient, client_id)
    if not c:
        raise HTTPException(404)
    notice, target = "", "/settings/downloadclients"
    if action == "delete":
        await session.delete(c)
        notice = "Download+client+deleted"
    elif action == "test":
        target = f"/settings/downloadclients/{client_id}"
        try:
            notice = "Test+succeeded:+" + (await downloadclients.test(c)).replace(" ", "+")
            c.last_error = None
        except Exception as exc:
            c.last_error = str(exc)[:300]
            notice = f"Test+failed:+{str(exc)[:80]}"
    await session.commit()
    return RedirectResponse(url(f"{target}?notice={notice}"), status_code=303)


# import lists


@protected.get("/settings/importlists", response_class=HTMLResponse)
async def settings_importlists(request: Request, session: AsyncSession = Depends(get_session)):
    from chordarr.services import importlists

    rows = (await session.scalars(select(ImportList).order_by(ImportList.id))).all()
    return render(request, "settings/importlists.html", lists=rows, LIST_TYPES=importlists.LIST_TYPES)


@protected.get("/settings/importlists/add", response_class=HTMLResponse)
async def importlist_add(request: Request):
    from chordarr.services import importlists

    presets = [(k, v["label"], v["help"]) for k, v in importlists.LIST_TYPES.items()]
    return render(request, "settings/provider_add.html", kind="importlist", title="Add Import List", presets=presets, new_url=url("/settings/importlists/new"), hint="")


@protected.get("/settings/importlists/new", response_class=HTMLResponse)
async def importlist_new(request: Request, implementation: str = "apple_music_charts"):
    from chordarr.services import importlists

    t = importlists.LIST_TYPES.get(implementation)
    if not t:
        raise HTTPException(404)
    lst = ImportList(name=t["label"] + (" (FR)" if "country" in t["fields"] else ""), type=implementation, config={"country": "fr", "count": 100})
    return render(request, "settings/importlist_edit.html", lst=lst, is_new=True, ltype=t)


@protected.get("/settings/importlists/{list_id}", response_class=HTMLResponse)
async def importlist_edit(request: Request, list_id: int, session: AsyncSession = Depends(get_session)):
    from chordarr.services import importlists

    lst = await session.get(ImportList, list_id)
    if not l:
        raise HTTPException(404)
    return render(request, "settings/importlist_edit.html", lst=lst, is_new=False, ltype=importlists.LIST_TYPES.get(lst.type, {"label": lst.type, "fields": [], "help": ""}))


@protected.post("/settings/importlists/save")
async def importlist_save(request: Request, session: AsyncSession = Depends(get_session)):
    from chordarr.services import importlists

    form = await request.form()
    lid, ltype = form.get("id"), str(form.get("type") or "")
    if ltype not in importlists.LIST_TYPES:
        raise HTTPException(400)
    lst = await session.get(ImportList, int(lid)) if lid else ImportList(name="", type=ltype, config={})
    if lst is None:
        raise HTTPException(404)
    lst.name = str(form.get("name") or lst.name or importlists.LIST_TYPES[ltype]["label"])
    lst.enabled = _form_bool(form, "enabled")
    lst.monitor = "artist" if form.get("monitor") == "artist" else "album"
    lst.search_on_add = _form_bool(form, "search_on_add")
    try:
        lst.interval_hours = max(1.0, float(form.get("interval_hours") or 24))
        count = max(10, min(int(form.get("count") or 100), 100))
    except ValueError:
        return RedirectResponse(url("/settings/importlists?notice=Invalid+number"), status_code=303)
    lst.config = {"country": str(form.get("country") or "fr").lower()[:2], "count": count}
    session.add(lst)
    await session.commit()
    return RedirectResponse(url("/settings/importlists?notice=List+saved"), status_code=303)


@protected.post("/settings/importlists/{list_id}/{action}")
async def importlist_action(list_id: int, action: str, session: AsyncSession = Depends(get_session)):
    from chordarr.services import importlists

    lst = await session.get(ImportList, list_id)
    if not lst:
        raise HTTPException(404)
    notice, target = "", "/settings/importlists"
    if action == "delete":
        await session.delete(lst)
        notice = "List+deleted"
    elif action == "test":
        target = f"/settings/importlists/{list_id}"
        try:
            notice = "Test+succeeded:+" + (await importlists.test(lst)).replace(" ", "+")
        except Exception as exc:
            notice = f"Test+failed:+{str(exc)[:80]}"
    elif action == "run":
        scheduler.trigger_import_list(list_id)
        notice = f"{lst.name.replace(' ', '+')}+is+running+in+the+background"
    await session.commit()
    return RedirectResponse(url(f"{target}?notice={notice}"), status_code=303)


# connect (notifications and media servers)

CONNECT_TYPES = {
    **{k: {"label": v["label"], "kind": "notifier", "fields": v["fields"]} for k, v in notify.NOTIFIER_TYPES.items()},
    **{k: {"label": v["label"], "kind": "mediaserver", "fields": v["fields"]} for k, v in mediaservers.MEDIA_SERVER_TYPES.items()},
}


@protected.get("/settings/connect", response_class=HTMLResponse)
async def settings_connect(request: Request, session: AsyncSession = Depends(get_session)):
    notifiers = (await session.scalars(select(Notifier).order_by(Notifier.id))).all()
    servers = (await session.scalars(select(MediaServer).order_by(MediaServer.id))).all()
    return render(request, "settings/connect.html", notifiers=notifiers, mediaservers=servers)


@protected.get("/settings/connect/add", response_class=HTMLResponse)
async def connect_add(request: Request):
    presets = [(k, v["label"], "Notification" if v["kind"] == "notifier" else "Media server: library scan after each import") for k, v in CONNECT_TYPES.items()]
    return render(request, "settings/provider_add.html", kind="connect", title="Add Connection", presets=presets, new_url=url("/settings/connect/new"), hint="")


@protected.get("/settings/connect/new", response_class=HTMLResponse)
async def connect_new(request: Request, implementation: str = "webhook"):
    t = CONNECT_TYPES.get(implementation)
    if not t:
        raise HTTPException(404)
    if t["kind"] == "notifier":
        n = Notifier(name=t["label"], type=implementation, config={}, events=[])
        return render(request, "settings/connect_edit.html", n=n, m=None, is_new=True, ctype=t)
    m = MediaServer(name=t["label"], type=implementation, url="", api_key="", local_path=str(settings.root_folder or ""), remote_path="")
    return render(request, "settings/connect_edit.html", n=None, m=m, is_new=True, ctype=t)


@protected.get("/settings/connect/{kind}/{item_id}", response_class=HTMLResponse)
async def connect_edit(request: Request, kind: str, item_id: int, session: AsyncSession = Depends(get_session)):
    if kind == "notifier":
        n = await session.get(Notifier, item_id)
        if not n:
            raise HTTPException(404)
        return render(request, "settings/connect_edit.html", n=n, m=None, is_new=False, ctype=CONNECT_TYPES.get(n.type, {"label": n.type, "fields": []}))
    m = await session.get(MediaServer, item_id)
    if not m:
        raise HTTPException(404)
    return render(request, "settings/connect_edit.html", n=None, m=m, is_new=False, ctype=CONNECT_TYPES.get(m.type, {"label": m.type, "fields": []}))


@protected.post("/settings/connect/save")
async def connect_save(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    kind, iid, ctype = form.get("kind"), form.get("id"), str(form.get("type") or "")
    if kind == "notifier":
        if ctype not in notify.NOTIFIER_TYPES:
            raise HTTPException(400)
        n = await session.get(Notifier, int(iid)) if iid else Notifier(name="", type=ctype, config={}, events=[])
        if n is None:
            raise HTTPException(404)
        cfg = dict(n.config or {})
        for f in notify.NOTIFIER_TYPES[ctype]["fields"]:
            v = form.get(f"cfg_{f}")
            if v is None or v == lidarr_compat.MASK:
                continue
            cfg[f] = str(v)
        n.name = str(form.get("name") or n.name or notify.NOTIFIER_TYPES[ctype]["label"])
        n.enabled = _form_bool(form, "enabled")
        n.config = cfg
        n.events = [e for e in EVENT_TYPES if form.get(f"ev_{e}")]
        session.add(n)
    else:
        if ctype not in mediaservers.MEDIA_SERVER_TYPES:
            raise HTTPException(400)
        m = await session.get(MediaServer, int(iid)) if iid else MediaServer(name="", type=ctype, url="")
        if m is None:
            raise HTTPException(404)
        m.name = str(form.get("name") or m.name or mediaservers.MEDIA_SERVER_TYPES[ctype]["label"])
        m.enabled = _form_bool(form, "enabled")
        m.url = str(form.get("url") or "").strip()
        if form.get("api_key") not in (None, "", lidarr_compat.MASK):
            m.api_key = str(form.get("api_key"))
        m.section = str(form.get("section") or "").strip()
        m.local_path = str(form.get("local_path") or "").strip()
        m.remote_path = str(form.get("remote_path") or "").strip()
        if not m.url:
            return RedirectResponse(url("/settings/connect?notice=URL+is+required"), status_code=303)
        session.add(m)
    await session.commit()
    return RedirectResponse(url("/settings/connect?notice=Connection+saved"), status_code=303)


@protected.post("/settings/connect/{kind}/{item_id}/{action}")
async def connect_action(kind: str, item_id: int, action: str, session: AsyncSession = Depends(get_session)):
    obj = await session.get(Notifier if kind == "notifier" else MediaServer, item_id)
    if not obj:
        raise HTTPException(404)
    notice, target = "", "/settings/connect"
    if action == "delete":
        await session.delete(obj)
        notice = "Connection+deleted"
    elif action == "test":
        target = f"/settings/connect/{kind}/{item_id}"
        try:
            if kind == "notifier":
                await notify.test(obj)
                notice = "Test+notification+sent"
            else:
                notice = "Test+succeeded:+" + (await mediaservers.test(obj)).replace(" ", "+")
                obj.last_error = None
        except Exception as exc:
            if kind != "notifier":
                obj.last_error = str(exc)[:300]
            notice = f"Test+failed:+{str(exc)[:80]}"
    await session.commit()
    return RedirectResponse(url(f"{target}?notice={notice}"), status_code=303)


# general


@protected.get("/settings/general", response_class=HTMLResponse)
async def settings_general(request: Request):
    return render(request, "settings/general.html", api_key=settings.api_key)


@protected.post("/settings/general")
async def settings_general_save(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    level = str(form.get("log_level") or "info").lower()
    await save_overrides(
        session,
        log_level=level if level in ("debug", "info", "warning", "error") else "info",
        backup_interval_days=form.get("backup_interval_days") or 7,
        refresh_interval_hours=form.get("refresh_interval_hours") or 12,
        scan_interval_hours=form.get("scan_interval_hours") or 6,
        recommendations_interval_hours=form.get("recommendations_interval_hours") or 24,
    )
    await session.commit()
    return RedirectResponse(url("/settings/general?notice=Settings+saved"), status_code=303)


router.include_router(protected)
