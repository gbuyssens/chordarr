"""Server-rendered UI (Jinja2, plain forms). No build step, no CDN: works offline."""

from __future__ import annotations

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
from chordarr.models import (
    Artist,
    Download,
    DownloadClient,
    Event,
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
from chordarr.services.prefs import QUALITY_PROFILES, get_prefs, set_prefs

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
)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"request": request, **ctx})


async def guard(request: Request):
    if request.url.path.startswith(f"{settings.url_base}/login") or request.url.path.startswith("/static"):
        return
    if not ui_authenticated(request):
        raise HTTPException(status_code=307, headers={"Location": f"{settings.url_base}/login"})


protected = APIRouter(dependencies=[Depends(guard)])


# --- login ------------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if ui_authenticated(request):
        return RedirectResponse(f"{settings.url_base}/", status_code=303)
    return render(request, "login.html", error=None)


@router.post("/login")
async def login(request: Request, password: str = Form(...)):
    import hmac

    if settings.ui_password and hmac.compare_digest(password, settings.ui_password):
        resp = RedirectResponse(f"{settings.url_base}/", status_code=303)
        resp.set_cookie(COOKIE, session_token(), httponly=True, samesite="lax", max_age=30 * 86400)
        return resp
    return render(request, "login.html", error="Wrong password")


@router.get("/logout")
async def logout():
    resp = RedirectResponse(f"{settings.url_base}/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# --- pages ------------------------------------------------------------------------------


@protected.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    today = date.today()
    upcoming = (
        await session.scalars(
            select(Release)
            .where(Release.monitored.is_(True), Release.first_release_date > today)
            .order_by(Release.first_release_date)
            .limit(12)
        )
    ).all()
    recent = (
        await session.scalars(
            select(Release)
            .where(Release.monitored.is_(True), Release.first_release_date <= today, Release.first_release_date >= today - timedelta(days=60))
            .order_by(Release.first_release_date.desc())
            .limit(12)
        )
    ).all()
    events = (await session.scalars(select(Event).order_by(Event.id.desc()).limit(15))).all()
    counts = {
        "artists": await session.scalar(select(func.count(Artist.id))),
        "releases": await session.scalar(select(func.count(Release.id)).where(Release.kind == "own")),
        "appearances": await session.scalar(select(func.count(Release.id)).where(Release.kind == "appearance")),
        "have": await session.scalar(select(func.count(Release.id)).where(Release.status == "have")),
        "wanted": await session.scalar(
            select(func.count(Release.id)).where(Release.monitored.is_(True), Release.status == "missing", Release.first_release_date <= today)
        ),
    }
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    return render(request, "dashboard.html", upcoming=upcoming, recent=recent, events=events, counts=counts, artists=artists, jobs=scheduler.status())


@protected.get("/artists", response_class=HTMLResponse)
async def artists_page(request: Request, session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Artist).order_by(Artist.sort_name))).all()
    return render(request, "artists.html", artists=rows)


@protected.get("/artists/add", response_class=HTMLResponse)
async def add_artist_page(request: Request, q: str = ""):
    results = await mb.search_artists(q) if q else []
    return render(request, "artist_add.html", q=q, results=results)


@protected.post("/artists/add")
async def add_artist_submit(
    request: Request,
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
        return RedirectResponse(f"{settings.url_base}/artists/{existing.id}?notice=already+tracked+({exc})", status_code=303)
    await library.scan_artist(session, artist)
    await session.commit()
    scheduler.wake()
    return RedirectResponse(f"{settings.url_base}/artists/{artist.id}", status_code=303)


@protected.get("/artists/{artist_id}", response_class=HTMLResponse)
async def artist_page(request: Request, artist_id: int, session: AsyncSession = Depends(get_session)):
    a = await session.get(Artist, artist_id)
    if not a:
        raise HTTPException(404)
    own = sorted((r for r in a.releases if r.kind == "own"), key=lambda r: (r.first_release_date or date.min), reverse=True)
    appearances = sorted((r for r in a.releases if r.kind == "appearance"), key=lambda r: (r.first_release_date or date.min), reverse=True)
    groups: dict[str, list[Release]] = {}
    for r in own:
        groups.setdefault(r.primary_type, []).append(r)
    order = ["Album", "EP", "Single", "Broadcast", "Other"]
    groups = {k: groups[k] for k in order if k in groups} | {k: v for k, v in groups.items() if k not in order}
    by_release = {}
    for ap in a.appearances:
        by_release.setdefault(ap.release_id, []).append(ap)
    return render(request, "artist.html", artist=a, groups=groups, appearances=appearances, tracks=by_release, folder=library.artist_folder(a), notice=request.query_params.get("notice"))


@protected.post("/artists/{artist_id}/toggle")
async def artist_toggle(artist_id: int, field: str = Form(...), session: AsyncSession = Depends(get_session)):
    a = await session.get(Artist, artist_id)
    if not a:
        raise HTTPException(404)
    if field == "monitored":
        a.monitored = not a.monitored
    elif field == "monitor_appearances":
        a.monitor_appearances = not a.monitor_appearances
    elif field in ("Album", "EP", "Single", "Broadcast", "Other"):
        types = list(a.monitor_types or [])
        a.monitor_types = [t for t in types if t != field] if field in types else types + [field]
    await session.commit()
    return RedirectResponse(f"{settings.url_base}/artists/{artist_id}", status_code=303)


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
    return RedirectResponse(f"{settings.url_base}/artists/{artist_id}", status_code=303)


@protected.post("/artists/{artist_id}/delete")
async def artist_delete(artist_id: int, session: AsyncSession = Depends(get_session)):
    a = await session.get(Artist, artist_id)
    if a:
        await session.delete(a)
        await session.commit()
    return RedirectResponse(f"{settings.url_base}/artists", status_code=303)


@protected.post("/releases/{release_id}/toggle")
async def release_toggle(request: Request, release_id: int, session: AsyncSession = Depends(get_session)):
    r = await session.get(Release, release_id)
    if not r:
        raise HTTPException(404)
    r.monitored = not r.monitored
    await session.commit()
    return RedirectResponse(request.headers.get("referer") or f"{settings.url_base}/artists/{r.artist_id}", status_code=303)


@protected.get("/calendar", response_class=HTMLResponse)
async def calendar_page(request: Request, session: AsyncSession = Depends(get_session)):
    today = date.today()
    rows = (
        await session.scalars(
            select(Release)
            .where(Release.monitored.is_(True), Release.first_release_date >= today - timedelta(days=90), Release.first_release_date <= today + timedelta(days=365))
            .order_by(Release.first_release_date.desc())
        )
    ).all()
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    months: dict[str, list[Release]] = {}
    for r in rows:
        months.setdefault(r.first_release_date.strftime("%B %Y"), []).append(r)
    return render(request, "calendar.html", months=months, artists=artists)


@protected.get("/wanted", response_class=HTMLResponse)
async def wanted_page(request: Request, session: AsyncSession = Depends(get_session)):
    rows = (
        await session.scalars(
            select(Release)
            .where(Release.monitored.is_(True), Release.status.in_(("missing", "partial")), Release.first_release_date <= date.today())
            .order_by(Release.first_release_date.desc())
        )
    ).all()
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    return render(request, "wanted.html", releases=rows, artists=artists)


@protected.get("/recommendations", response_class=HTMLResponse)
async def recommendations_page(request: Request, session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Recommendation).where(Recommendation.dismissed.is_(False)).order_by(Recommendation.score.desc()).limit(60))).all()
    return render(request, "recommendations.html", recs=rows, jobs=scheduler.status())


@protected.post("/recommendations/{rec_id}/{action}")
async def recommendation_action(rec_id: int, action: str, session: AsyncSession = Depends(get_session)):
    r = await session.get(Recommendation, rec_id)
    if not r:
        raise HTTPException(404)
    if action == "dismiss":
        r.dismissed = True
        await session.commit()
        return RedirectResponse(f"{settings.url_base}/recommendations", status_code=303)
    mbid = r.mbid
    if not mbid:
        found = await mb.search_artists(r.name, limit=5)
        exact = [f for f in found if f.name.casefold() == r.name.casefold()]
        if not exact:
            return RedirectResponse(f"{settings.url_base}/artists/add?q={r.name}", status_code=303)
        mbid = exact[0].mbid
    try:
        a = await artist_service.add_artist(session, mbid)
    except artist_service.ArtistExists:
        a = await session.scalar(select(Artist).where(Artist.mbid == mbid))
    await session.delete(r)
    await session.commit()
    scheduler.wake()
    return RedirectResponse(f"{settings.url_base}/artists/{a.id}", status_code=303)


@protected.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request, session: AsyncSession = Depends(get_session)):
    events = (await session.scalars(select(Event).order_by(Event.id.desc()).limit(200))).all()
    return render(request, "activity.html", events=events)


@protected.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: AsyncSession = Depends(get_session)):
    notifiers = (await session.scalars(select(Notifier).order_by(Notifier.id))).all()
    known = {a.path for a in (await session.scalars(select(Artist))).all() if a.path}
    unknown = library.unknown_artist_folders(known)
    return render(
        request,
        "settings.html",
        notifiers=notifiers,
        unknown=unknown,
        jobs=scheduler.status(),
        notice=request.query_params.get("notice"),
        api_key=settings.api_key,
        prefs=await get_prefs(session),
        indexers=(await session.scalars(select(Indexer).order_by(Indexer.priority, Indexer.id))).all(),
        clients=(await session.scalars(select(DownloadClient).order_by(DownloadClient.id))).all(),
        mediaservers=(await session.scalars(select(MediaServer).order_by(MediaServer.id))).all(),
    )


@protected.post("/settings/notifiers")
async def notifier_create(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    ntype = form.get("type")
    if ntype not in notify.NOTIFIER_TYPES:
        raise HTTPException(400)
    cfg = {f: form.get(f"cfg_{f}", "") for f in notify.NOTIFIER_TYPES[ntype]["fields"]}
    events = [e for e in EVENT_TYPES if form.get(f"ev_{e}")]
    n = Notifier(name=form.get("name") or notify.NOTIFIER_TYPES[ntype]["label"], type=ntype, config=cfg, events=events)
    session.add(n)
    await session.commit()
    return RedirectResponse(f"{settings.url_base}/settings?notice=notifier+added", status_code=303)


@protected.post("/settings/notifiers/{notifier_id}/{action}")
async def notifier_action(notifier_id: int, action: str, session: AsyncSession = Depends(get_session)):
    n = await session.get(Notifier, notifier_id)
    if not n:
        raise HTTPException(404)
    notice = ""
    if action == "delete":
        await session.delete(n)
        notice = "notifier+removed"
    elif action == "toggle":
        n.enabled = not n.enabled
    elif action == "test":
        try:
            await notify.test(n)
            notice = "test+sent"
        except Exception as exc:
            notice = f"test+failed:+{str(exc)[:80]}"
    await session.commit()
    return RedirectResponse(f"{settings.url_base}/settings?notice={notice}", status_code=303)


@protected.post("/settings/import/lidarr")
async def import_lidarr_ui(
    db_path: str = Form(""),
    url: str = Form(""),
    api_key: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    import asyncio

    try:
        if db_path:
            rows = await asyncio.to_thread(lidarr_import.read_database, db_path)
        elif url and api_key:
            rows = await lidarr_import.read_api(url, api_key)
        else:
            raise ValueError("give a database path, or a URL and an API key")
    except Exception as exc:
        return RedirectResponse(f"{settings.url_base}/settings?notice=import+failed:+{str(exc)[:100]}", status_code=303)
    added = skipped = 0
    for row in rows:
        try:
            await artist_service.add_artist(session, row.mbid, path=row.path, monitored=row.monitored, monitor_types=["Album"], refresh=False)
            added += 1
            await session.commit()
        except (artist_service.ArtistExists, ValueError):
            skipped += 1
    scheduler.trigger("refresh")
    return RedirectResponse(f"{settings.url_base}/settings?notice={added}+artists+imported,+{skipped}+skipped;+refresh+running", status_code=303)


@protected.post("/settings/jobs/{name}")
async def run_job(request: Request, name: str):
    if name not in ("refresh", "scan", "recommendations", "notify", "rss", "search", "queue"):
        raise HTTPException(404)
    scheduler.trigger(name)
    back = request.headers.get("referer") or f"{settings.url_base}/settings"
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back.split('?notice=')[0]}{sep}notice={name}+started", status_code=303)


# --- acquisition settings ---------------------------------------------------------------


def _settings_redirect(anchor: str, notice: str) -> RedirectResponse:
    return RedirectResponse(f"{settings.url_base}/settings?notice={notice}#{anchor}", status_code=303)


@protected.post("/settings/acquisition")
async def acquisition_prefs(auto_grab: bool = Form(False), quality_profile: str = Form("lossless"), session: AsyncSession = Depends(get_session)):
    if quality_profile not in QUALITY_PROFILES:
        raise HTTPException(400)
    await set_prefs(session, auto_grab=auto_grab, quality_profile=quality_profile)
    await session.commit()
    if auto_grab:
        scheduler.wake()
    return _settings_redirect("acquisition", "acquisition+settings+saved")


@protected.post("/settings/indexers")
async def indexer_create(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    try:
        cats = [int(c) for c in str(form.get("categories", "3000")).replace(";", ",").split(",") if c.strip()]
        i = Indexer(
            name=form.get("name") or "Indexer",
            implementation="Newznab" if form.get("implementation") == "Newznab" else "Torznab",
            base_url=str(form.get("base_url", "")).strip(),
            api_path=str(form.get("api_path") or "/api"),
            api_key=str(form.get("api_key", "")),
            categories=cats or [3000],
            priority=int(form.get("priority") or 25),
            minimum_seeders=int(form.get("minimum_seeders") or 0),
        )
    except ValueError as exc:
        return _settings_redirect("indexers", f"invalid+indexer:+{str(exc)[:80]}")
    if not i.base_url:
        return _settings_redirect("indexers", "indexer+URL+is+required")
    try:
        i.capabilities = await indexers.caps(i)
    except Exception as exc:
        i.last_error = str(exc)[:300]
    session.add(i)
    await session.commit()
    return _settings_redirect("indexers", "indexer+added" if not i.last_error else "indexer+added,+but+unreachable")


@protected.post("/settings/indexers/{indexer_id}/{action}")
async def indexer_action(indexer_id: int, action: str, session: AsyncSession = Depends(get_session)):
    i = await session.get(Indexer, indexer_id)
    if not i:
        raise HTTPException(404)
    notice = ""
    if action == "delete":
        await session.delete(i)
        notice = "indexer+removed"
    elif action == "toggle":
        on = not (i.enable_rss or i.enable_automatic_search or i.enable_interactive_search)
        i.enable_rss = i.enable_automatic_search = i.enable_interactive_search = on
    elif action == "test":
        try:
            i.capabilities = await indexers.test(i)
            i.last_error = None
            notice = f"{i.name}:+OK,+music+search+params+{','.join(i.capabilities.get('music_params') or ['none'])}"
        except Exception as exc:
            i.last_error = str(exc)[:300]
            notice = f"test+failed:+{str(exc)[:80]}"
    await session.commit()
    return _settings_redirect("indexers", notice)


@protected.post("/settings/clients")
async def client_create(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    impl = form.get("implementation")
    if impl not in downloadclients.CLIENT_TYPES:
        raise HTTPException(400)
    c = DownloadClient(
        name=form.get("name") or downloadclients.CLIENT_TYPES[impl]["label"],
        implementation=impl,
        url=str(form.get("url", "")).strip(),
        username=str(form.get("username", "")),
        password=str(form.get("password", "")),
        api_key=str(form.get("api_key", "")),
        category=str(form.get("category") or "chordarr"),
        remote_path=str(form.get("remote_path", "")).strip(),
        local_path=str(form.get("local_path", "")).strip(),
        remove_completed=bool(form.get("remove_completed")),
    )
    if not c.url:
        return _settings_redirect("clients", "client+URL+is+required")
    session.add(c)
    await session.commit()
    return _settings_redirect("clients", "download+client+added")


@protected.post("/settings/clients/{client_id}/{action}")
async def client_action(client_id: int, action: str, session: AsyncSession = Depends(get_session)):
    c = await session.get(DownloadClient, client_id)
    if not c:
        raise HTTPException(404)
    notice = ""
    if action == "delete":
        await session.delete(c)
        notice = "download+client+removed"
    elif action == "toggle":
        c.enabled = not c.enabled
    elif action == "test":
        try:
            notice = (await downloadclients.test(c)).replace(" ", "+") + "+reachable"
            c.last_error = None
        except Exception as exc:
            c.last_error = str(exc)[:300]
            notice = f"test+failed:+{str(exc)[:80]}"
    await session.commit()
    return _settings_redirect("clients", notice)


@protected.post("/settings/mediaservers")
async def mediaserver_create(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    kind = form.get("type")
    if kind not in mediaservers.MEDIA_SERVER_TYPES:
        raise HTTPException(400)
    m = MediaServer(
        name=form.get("name") or mediaservers.MEDIA_SERVER_TYPES[kind]["label"],
        type=kind,
        url=str(form.get("url", "")).strip(),
        api_key=str(form.get("api_key", "")),
        section=str(form.get("section", "")).strip(),
        local_path=str(form.get("local_path", "")).strip(),
        remote_path=str(form.get("remote_path", "")).strip(),
    )
    if not m.url:
        return _settings_redirect("mediaservers", "media+server+URL+is+required")
    session.add(m)
    await session.commit()
    return _settings_redirect("mediaservers", "media+server+added")


@protected.post("/settings/mediaservers/{server_id}/{action}")
async def mediaserver_action(server_id: int, action: str, session: AsyncSession = Depends(get_session)):
    m = await session.get(MediaServer, server_id)
    if not m:
        raise HTTPException(404)
    notice = ""
    if action == "delete":
        await session.delete(m)
        notice = "media+server+removed"
    elif action == "toggle":
        m.enabled = not m.enabled
    elif action == "test":
        try:
            notice = (await mediaservers.test(m)).replace(" ", "+")
            m.last_error = None
        except Exception as exc:
            m.last_error = str(exc)[:300]
            notice = f"test+failed:+{str(exc)[:80]}"
    await session.commit()
    return _settings_redirect("mediaservers", notice)


# --- search, grab, queue ----------------------------------------------------------------


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
    return render(request, "search.html", release=r, artist=artist, candidates=candidates, indexers=[i for i in idx if i.enable_interactive_search], prefs=prefs, notice=request.query_params.get("notice"))


@protected.post("/releases/{release_id}/grab")
async def release_grab_submit(release_id: int, guid: str = Form(...), indexer_id: int = Form(...), session: AsyncSession = Depends(get_session)):
    r = await session.get(Release, release_id)
    if not r:
        raise HTTPException(404)
    candidates = await acquisition.search_release(session, r)
    chosen = next((c for c in candidates if c.guid == guid and c.indexer_id == indexer_id), None)
    if chosen is None:
        return RedirectResponse(f"{settings.url_base}/releases/{release_id}/search?notice=that+result+is+gone,+search+again", status_code=303)
    try:
        await acquisition.grab(session, r, chosen, manual=True)
    except acquisition.GrabError as exc:
        await session.commit()
        return RedirectResponse(f"{settings.url_base}/releases/{release_id}/search?notice=grab+failed:+{str(exc)[:120]}", status_code=303)
    await session.commit()
    scheduler.wake()
    return RedirectResponse(f"{settings.url_base}/queue?notice=sent+to+the+download+client", status_code=303)


@protected.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, session: AsyncSession = Depends(get_session)):
    downloads = (await session.scalars(select(Download).order_by(Download.id.desc()).limit(200))).all()
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    wanted = (
        await session.scalars(
            select(Release).where(Release.status.in_(("missing", "partial")), Release.first_release_date <= date.today()).order_by(Release.first_release_date.desc()).limit(400)
        )
    ).all()
    wanted.sort(key=lambda r: (artists[r.artist_id].sort_name if r.artist_id in artists else "", r.title))
    clients = (await session.scalars(select(DownloadClient).where(DownloadClient.enabled.is_(True)))).all()
    return render(request, "queue.html", downloads=downloads, artists=artists, wanted=wanted, clients=clients, prefs=await get_prefs(session), jobs=scheduler.status(), notice=request.query_params.get("notice"))


@protected.post("/queue/refresh")
async def queue_refresh():
    scheduler.trigger("queue")
    return RedirectResponse(f"{settings.url_base}/queue?notice=checking+downloads", status_code=303)


@protected.post("/queue/{download_id}/remove")
async def queue_remove_ui(download_id: int, session: AsyncSession = Depends(get_session)):
    d = await session.get(Download, download_id)
    if d:
        cfg = await session.get(DownloadClient, d.download_client_id) if d.download_client_id else None
        if cfg and d.client_id and d.status != "imported":
            try:
                async with downloadclients.make_client(cfg) as dc:
                    await dc.remove(d.client_id, delete_files=False)
            except Exception:
                pass
        await session.delete(d)
        await session.commit()
    return RedirectResponse(f"{settings.url_base}/queue?notice=removed", status_code=303)


@protected.post("/queue/{download_id}/import")
async def queue_import_ui(download_id: int, force: bool = Form(False), session: AsyncSession = Depends(get_session)):
    d = await session.get(Download, download_id)
    if not d or not d.release or not d.output_path:
        return RedirectResponse(f"{settings.url_base}/queue?notice=nothing+to+import+for+this+entry", status_code=303)
    try:
        result = await importer.import_folder(session, Path(d.output_path), d.release, mode=importer.effective_mode(d.protocol), force=force)
    except importer.ImportError_ as exc:
        d.status, d.message = "warning", f"import needs attention: {exc}"
        await session.commit()
        return RedirectResponse(f"{settings.url_base}/queue?notice=import+failed:+{str(exc)[:120]}", status_code=303)
    d.status, d.progress, d.imported_at = "imported", 1.0, importer.utcnow()
    d.message = f"{len(result.files)} files → {result.destination}"
    await session.commit()
    return RedirectResponse(f"{settings.url_base}/queue?notice=imported+{len(result.files)}+files", status_code=303)


@protected.post("/import/manual")
async def manual_import_ui(
    path: str = Form(...),
    release_id: str = Form(""),
    mode: str = Form(""),
    force: bool = Form(False),
    session: AsyncSession = Depends(get_session),
):
    src = Path(path.strip())
    release = await session.get(Release, int(release_id)) if release_id else await importer.guess_release(session, src)
    if release is None:
        return RedirectResponse(f"{settings.url_base}/queue?notice=could+not+tell+which+release+this+is;+pick+one#manual", status_code=303)
    try:
        result = await importer.import_folder(session, src, release, mode=mode or importer.effective_mode(None), force=force)
    except importer.ImportError_ as exc:
        return RedirectResponse(f"{settings.url_base}/queue?notice=import+failed:+{str(exc)[:140]}#manual", status_code=303)
    await session.commit()
    return RedirectResponse(f"{settings.url_base}/artists/{release.artist_id}?notice=imported+{len(result.files)}+files+into+{result.destination.name}", status_code=303)


router.include_router(protected)
