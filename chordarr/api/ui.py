"""Server-rendered UI, laid out like the other *arr applications.

Sidebar: Artists (Add New, Library Import, Discover), Calendar, Activity (Queue, History),
Wanted (Missing), Settings (Media Management, Connect, General), System (Status, Tasks).
Jinja2 templates, plain forms, no build step, no CDN.
"""

from __future__ import annotations

import asyncio
import hmac
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from chordarr import __version__
from chordarr.api.auth import COOKIE, session_token, ui_authenticated
from chordarr.config import settings
from chordarr.db import get_session
from chordarr.metadata import musicbrainz as mb
from chordarr.models import Artist, Event, Notifier, Recommendation, Release
from chordarr.services import artists as artist_service
from chordarr.services import library, lidarr_import, notify, scheduler
from chordarr.services.events import EVENT_TYPES

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
templates.env.globals.update(
    version=__version__,
    settings=settings,
    EVENT_TYPES=EVENT_TYPES,
    NOTIFIER_TYPES=notify.NOTIFIER_TYPES,
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
    session: AsyncSession = Depends(get_session),
):
    types = [t for t, on in (("Album", monitor_album), ("EP", monitor_ep), ("Single", monitor_single)) if on]
    try:
        artist = await artist_service.add_artist(session, mbid, monitor_types=types or ["Album"], monitor_appearances=monitor_appearances)
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
async def queue_page(request: Request):
    return render(request, "activity/queue.html")


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


@protected.get("/settings")
async def settings_root():
    return RedirectResponse(url("/settings/mediamanagement"), status_code=302)


@protected.get("/settings/mediamanagement", response_class=HTMLResponse)
async def settings_media(request: Request):
    import shutil

    root = settings.root_folder
    free = shutil.disk_usage(root).free if root and root.is_dir() else None
    return render(request, "settings/mediamanagement.html", root=root, free=free)


@protected.get("/settings/connect", response_class=HTMLResponse)
async def settings_connect(request: Request, session: AsyncSession = Depends(get_session)):
    notifiers = (await session.scalars(select(Notifier).order_by(Notifier.id))).all()
    return render(request, "settings/connect.html", notifiers=notifiers)


@protected.post("/settings/connect")
async def notifier_create(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    ntype = form.get("type")
    if ntype not in notify.NOTIFIER_TYPES:
        raise HTTPException(400)
    cfg = {f: form.get(f"cfg_{f}", "") for f in notify.NOTIFIER_TYPES[ntype]["fields"]}
    events = [e for e in EVENT_TYPES if form.get(f"ev_{e}")]
    session.add(Notifier(name=form.get("name") or notify.NOTIFIER_TYPES[ntype]["label"], type=ntype, config=cfg, events=events))
    await session.commit()
    return RedirectResponse(url("/settings/connect?notice=Connection+added"), status_code=303)


@protected.post("/settings/connect/{notifier_id}/{action}")
async def notifier_action(notifier_id: int, action: str, session: AsyncSession = Depends(get_session)):
    n = await session.get(Notifier, notifier_id)
    if not n:
        raise HTTPException(404)
    notice = ""
    if action == "delete":
        await session.delete(n)
        notice = "Connection+removed"
    elif action == "toggle":
        n.enabled = not n.enabled
    elif action == "test":
        try:
            await notify.test(n)
            notice = "Test+sent"
        except Exception as exc:
            notice = f"Test+failed:+{str(exc)[:80]}"
    await session.commit()
    return RedirectResponse(url(f"/settings/connect?notice={notice}"), status_code=303)


@protected.get("/settings/general", response_class=HTMLResponse)
async def settings_general(request: Request):
    return render(request, "settings/general.html", api_key=settings.api_key)


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
        ("notify", "Send notifications", "every 15 min", "dispatch pending events to connections"),
    ]
    return render(request, "system/tasks.html", tasks=tasks)


@protected.post("/system/tasks/{name}")
async def run_task(name: str):
    if name not in ("refresh", "scan", "recommendations", "notify"):
        raise HTTPException(404)
    scheduler.trigger(name)
    return RedirectResponse(url(f"/system/tasks?notice={name}+started"), status_code=303)


router.include_router(protected)
