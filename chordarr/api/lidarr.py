"""Lidarr-shaped views of Chordarr data.

Third-party tools (Prowlarr, nzb360, LunaSea, Homarr, Organizr, Unpackerr, Buildarr…)
speak Lidarr's API v1. Chordarr answers them with the same paths and the same field
names, as a *superset* of its own payloads: ``/api/v1/artist`` carries both ``name`` and
``artistName``, both ``mbid`` and ``foreignArtistId``. This module holds the mapping and
the endpoints that exist only for compatibility (``/album``, ``/wanted/missing``,
``/history``, ``/command``, ``/metadataprofile``, ``/config/*``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chordarr import __version__
from chordarr.api.auth import require_api_key
from chordarr.config import settings
from chordarr.db import get_session
from chordarr.models import Artist, Event, Release
from chordarr.services import scheduler

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)], tags=["lidarr-compatible"])

QUALITY_PROFILE_ID = 1
METADATA_PROFILE_ID = 1
ROOT_FOLDER_ID = 1

HISTORY_EVENT_TYPES = {
    # Lidarr: 1 grabbed, 2 artistFolderImported, 3 trackFileImported, 4 downloadFailed,
    # 5 trackFileDeleted, 6 trackFileRenamed, 7 albumImportIncomplete, 8 downloadImported,
    # 9 trackFileRetagged, 10 downloadIgnored
    "release_imported": "albumImportIncomplete",
    "grabbed": "grabbed",
    "download_failed": "downloadFailed",
}


def _iso(d: datetime | date | None) -> str | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.replace(microsecond=0).isoformat() + "Z"
    return datetime(d.year, d.month, d.day).isoformat() + "Z"


def artist_payload(a: Artist) -> dict:
    own = [r for r in a.releases if r.kind == "own"]
    monitored = [r for r in own if r.monitored]
    have = [r for r in monitored if r.status == "have"]
    files = sum(r.file_count for r in a.releases)
    images = [{"coverType": "poster", "url": a.image_url, "remoteUrl": a.image_url}] if a.image_url else []
    return {
        "id": a.id,
        "artistName": a.name,
        "sortName": a.sort_name or a.name,
        "foreignArtistId": a.mbid,
        "mbId": a.mbid,
        "artistType": a.type,
        "disambiguation": a.disambiguation,
        "status": "continuing",
        "ended": False,
        "overview": "",
        "monitored": a.monitored,
        "monitorNewItems": getattr(a, "monitor_new_items", "all"),
        "qualityProfileId": QUALITY_PROFILE_ID,
        "metadataProfileId": METADATA_PROFILE_ID,
        "path": a.path or "",
        "rootFolderPath": str(settings.root_folder) if settings.root_folder else "",
        "folder": (a.path or "").rsplit("/", 1)[-1],
        "cleanName": "".join(ch for ch in a.name.casefold() if ch.isalnum()),
        "added": _iso(a.added_at),
        "genres": [],
        "tags": [],
        "images": images,
        "links": [{"url": f"https://musicbrainz.org/artist/{a.mbid}", "name": "MusicBrainz"}]
        + ([{"url": f"https://www.deezer.com/artist/{a.deezer_id}", "name": "Deezer"}] if a.deezer_id else []),
        "statistics": {
            "albumCount": len(monitored),
            "trackFileCount": files,
            "trackCount": files,
            "totalTrackCount": files,
            "sizeOnDisk": 0,
            "percentOfTracks": round(100 * len(have) / len(monitored), 1) if monitored else 0,
        },
        "ratings": {"votes": 0, "value": 0},
    }


def album_payload(r: Release, artist: Artist | None = None, include_artist: bool = True) -> dict:
    types = {"Album": "Album", "EP": "EP", "Single": "Single", "Broadcast": "Broadcast", "Other": "Other"}
    out = {
        "id": r.id,
        "title": r.title,
        "disambiguation": "",
        "overview": "",
        "artistId": r.artist_id,
        "foreignAlbumId": r.mbid,
        "monitored": r.monitored,
        "anyReleaseOk": True,
        "profileId": QUALITY_PROFILE_ID,
        "duration": 0,
        "albumType": types.get(r.primary_type, "Other"),
        "secondaryTypes": list(r.secondary_types or []),
        "mediumCount": 1,
        "ratings": {"votes": 0, "value": 0},
        "releaseDate": _iso(r.first_release_date),
        "releases": [],
        "genres": [],
        "media": [],
        "images": [{"coverType": "cover", "url": r.cover_url, "remoteUrl": r.cover_url}] if r.cover_url else [],
        "links": [{"url": f"https://musicbrainz.org/release-group/{r.mbid}", "name": "MusicBrainz"}],
        "statistics": {
            "trackFileCount": r.file_count,
            "trackCount": r.track_count or r.file_count,
            "totalTrackCount": r.track_count or r.file_count,
            "sizeOnDisk": 0,
            "percentOfTracks": 100 if r.status == "have" else (round(100 * r.file_count / r.track_count, 1) if r.track_count and r.file_count else 0),
        },
        "grabbed": False,
        # Chordarr extras, harmless for Lidarr clients
        "kind": r.kind,
        "artistCredit": r.artist_credit,
        "status": r.status,
        "path": r.path,
    }
    if include_artist and artist is not None:
        out["artist"] = artist_payload(artist)
    return out


def paged(records: list, page: int, page_size: int, sort_key: str, sort_dir: str, total: int | None = None) -> dict:
    return {
        "page": page,
        "pageSize": page_size,
        "sortKey": sort_key,
        "sortDirection": sort_dir,
        "totalRecords": total if total is not None else len(records),
        "records": records,
    }


# --- albums -----------------------------------------------------------------------------


@router.get("/album")
async def albums(
    artistId: int | None = None,
    foreignAlbumId: str | None = None,
    albumIds: str | None = None,
    includeAllArtistAlbums: bool = False,
    kind: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    q = select(Release)
    if artistId is not None:
        q = q.where(Release.artist_id == artistId)
    if foreignAlbumId:
        q = q.where(Release.mbid == foreignAlbumId)
    if albumIds:
        q = q.where(Release.id.in_([int(x) for x in albumIds.split(",") if x.strip().isdigit()]))
    if kind:
        q = q.where(Release.kind == kind)
    elif not includeAllArtistAlbums and artistId is None and not foreignAlbumId and not albumIds:
        q = q.where(Release.monitored.is_(True))
    rows = (await session.scalars(q.order_by(Release.first_release_date.desc()))).all()
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    return [album_payload(r, artists.get(r.artist_id)) for r in rows]


@router.get("/album/{album_id}")
async def album(album_id: int, session: AsyncSession = Depends(get_session)):
    r = await session.get(Release, album_id)
    if not r:
        raise HTTPException(404)
    return album_payload(r, await session.get(Artist, r.artist_id))


class AlbumMonitorIn(BaseModel):
    albumIds: list[int]
    monitored: bool


@router.put("/album/monitor")
async def album_monitor(body: AlbumMonitorIn, session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Release).where(Release.id.in_(body.albumIds)))).all()
    for r in rows:
        r.monitored = body.monitored
    await session.commit()
    return [album_payload(r, None, include_artist=False) for r in rows]


@router.put("/album/{album_id}")
async def album_update(album_id: int, body: dict, session: AsyncSession = Depends(get_session)):
    r = await session.get(Release, album_id)
    if not r:
        raise HTTPException(404)
    if "monitored" in body:
        r.monitored = bool(body["monitored"])
    await session.commit()
    return album_payload(r, await session.get(Artist, r.artist_id))


# --- wanted -----------------------------------------------------------------------------


@router.get("/wanted/missing")
async def wanted_missing(
    page: int = 1,
    pageSize: int = 50,
    sortKey: str = "releaseDate",
    sortDirection: str = "descending",
    monitored: bool = True,
    includeArtist: bool = False,
    session: AsyncSession = Depends(get_session),
):
    q = select(Release).where(Release.status.in_(("missing", "partial")), Release.first_release_date <= date.today())
    if monitored:
        q = q.where(Release.monitored.is_(True))
    rows = (await session.scalars(q)).all()
    reverse = sortDirection.lower().startswith("desc")
    if sortKey in ("releaseDate", "albums.releaseDate"):
        rows.sort(key=lambda r: r.first_release_date or date.min, reverse=reverse)
    elif sortKey in ("title", "albums.title"):
        rows.sort(key=lambda r: r.title.casefold(), reverse=reverse)
    total = len(rows)
    chunk = rows[(page - 1) * pageSize : page * pageSize]
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    return paged([album_payload(r, artists.get(r.artist_id), include_artist=True) for r in chunk], page, pageSize, sortKey, sortDirection, total)


@router.get("/wanted/cutoff")
async def wanted_cutoff(page: int = 1, pageSize: int = 50, sortKey: str = "releaseDate", sortDirection: str = "descending"):
    return paged([], page, pageSize, sortKey, sortDirection, 0)


# --- history ----------------------------------------------------------------------------


def _history_record(e: Event, artists: dict[int, Artist], releases: dict[int, Release]) -> dict:
    rel = releases.get(e.release_id) if e.release_id else None
    art = artists.get(e.artist_id) if e.artist_id else None
    return {
        "id": e.id,
        "albumId": e.release_id or 0,
        "artistId": e.artist_id or 0,
        "trackId": 0,
        "sourceTitle": e.title,
        "quality": {"quality": {"id": 0, "name": "Unknown"}, "revision": {"version": 1, "real": 0, "isRepack": False}},
        "qualityCutoffNotMet": False,
        "date": _iso(e.created_at),
        "downloadId": (e.payload or {}).get("download_id"),
        "eventType": HISTORY_EVENT_TYPES.get(e.type, e.type),
        "data": {k: str(v) for k, v in (e.payload or {}).items() if not isinstance(v, (dict, list))} | {"message": e.body},
        "album": album_payload(rel, None, include_artist=False) if rel else None,
        "artist": artist_payload(art) if art else None,
    }


@router.get("/history")
async def history(
    page: int = 1,
    pageSize: int = 20,
    sortKey: str = "date",
    sortDirection: str = "descending",
    eventType: str | None = None,
    artistId: int | None = None,
    albumId: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    q = select(Event)
    if artistId is not None:
        q = q.where(Event.artist_id == artistId)
    if albumId is not None:
        q = q.where(Event.release_id == albumId)
    rows = (await session.scalars(q.order_by(Event.id.desc() if sortDirection.lower().startswith("desc") else Event.id))).all()
    if eventType:
        rows = [e for e in rows if HISTORY_EVENT_TYPES.get(e.type, e.type) == eventType or e.type == eventType]
    total = len(rows)
    chunk = rows[(page - 1) * pageSize : page * pageSize]
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    releases = {r.id: r for r in (await session.scalars(select(Release).where(Release.id.in_([e.release_id for e in chunk if e.release_id] or [0])))).all()}
    return paged([_history_record(e, artists, releases) for e in chunk], page, pageSize, sortKey, sortDirection, total)


@router.get("/history/since")
async def history_since(date: datetime, eventType: str | None = None, session: AsyncSession = Depends(get_session)):  # noqa: A002
    rows = (await session.scalars(select(Event).where(Event.created_at >= date.replace(tzinfo=None)).order_by(Event.id))).all()
    if eventType:
        rows = [e for e in rows if HISTORY_EVENT_TYPES.get(e.type, e.type) == eventType or e.type == eventType]
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    releases = {r.id: r for r in (await session.scalars(select(Release))).all()}
    return [_history_record(e, artists, releases) for e in rows]


# --- commands ---------------------------------------------------------------------------

COMMANDS = {
    "RefreshArtist": "refresh",
    "RescanFolders": "scan",
    "RescanArtist": "scan",
    "RssSync": "rss",
    "MissingAlbumSearch": "search",
    "AlbumSearch": "search",
    "ArtistSearch": "search",
    "ApplicationUpdateCheck": "update",
    "Backup": "backup",
    "RefreshRecommendations": "recommendations",
}
_commands: dict[int, dict] = {}
_next_command = [1]


def _record_command(name: str, body: dict, status: str) -> dict:
    cid = _next_command[0]
    _next_command[0] += 1
    cmd = {
        "id": cid,
        "name": name,
        "commandName": name,
        "body": body,
        "priority": "normal",
        "status": status,
        "queued": _iso(datetime.now(UTC).replace(tzinfo=None)),
        "started": _iso(datetime.now(UTC).replace(tzinfo=None)) if status != "failed" else None,
        "trigger": "manual",
        "sendUpdatesToClient": True,
        "updateScheduledTask": True,
        "lastExecutionTime": _iso(datetime.now(UTC).replace(tzinfo=None)),
    }
    _commands[cid] = cmd
    if len(_commands) > 200:
        del _commands[min(_commands)]
    return cmd


@router.get("/command")
async def list_commands():
    return list(_commands.values())[-50:]


@router.get("/command/{command_id}")
async def get_command(command_id: int):
    cmd = _commands.get(command_id)
    if not cmd:
        raise HTTPException(404)
    cmd = {**cmd, "status": "completed" if cmd["name"].replace("Artist", "") not in scheduler.status()["running"] else "started"}
    return cmd


@router.post("/command", status_code=201)
async def run_command(body: dict, session: AsyncSession = Depends(get_session)):
    name = body.get("name") or ""
    job = COMMANDS.get(name)
    if job is None:
        return _record_command(name, body, "failed")
    if job == "refresh" and body.get("artistId"):
        from chordarr.services import artists as artist_service
        from chordarr.services import library

        a = await session.get(Artist, int(body["artistId"]))
        if a:
            await artist_service.refresh_artist(session, a, announce=True)
            await session.refresh(a, ["releases"])
            await library.scan_artist(session, a)
            await session.commit()
            scheduler.trigger("notify")
        return _record_command(name, body, "completed")
    if job in ("refresh", "scan", "recommendations", "notify"):
        scheduler.trigger(job)
        return _record_command(name, body, "started")
    if job == "update":
        from chordarr.services import updates

        await updates.check(force=True)
        return _record_command(name, body, "completed")
    if job == "backup":
        from chordarr.services import backup

        await backup.create("manual")
        return _record_command(name, body, "completed")
    # rss / search: acquisition hooks (0.2) register themselves here when present.
    hook = getattr(scheduler, "acquisition_hooks", {}).get(job)
    if hook:
        await hook(body)
        return _record_command(name, body, "started")
    return _record_command(name, body, "completed")


# --- profiles and config ----------------------------------------------------------------


@router.get("/metadataprofile")
async def metadata_profiles():
    return [
        {
            "id": METADATA_PROFILE_ID,
            "name": "Standard",
            "primaryAlbumTypes": [{"albumType": {"id": i, "name": n}, "allowed": True} for i, n in enumerate(["Album", "EP", "Single", "Broadcast", "Other"])],
            "secondaryAlbumTypes": [{"albumType": {"id": 0, "name": "Studio"}, "allowed": True}],
            "releaseStatuses": [{"releaseStatus": {"id": 0, "name": "Official"}, "allowed": True}],
        }
    ]


@router.get("/config/host")
async def config_host():
    return {
        "id": 1,
        "bindAddress": settings.host,
        "port": settings.port,
        "urlBase": settings.url_base,
        "instanceName": "Chordarr",
        "enableSsl": False,
        "authenticationMethod": "external" if not settings.ui_password else "forms",
        "authenticationRequired": "disabledForLocalAddresses" if settings.auth_disabled_for_local else "enabled",
        "logLevel": settings.log_level,
        "launchBrowser": False,
        "analyticsEnabled": False,
        "branch": "main",
        "apiKey": settings.api_key,
    }


@router.get("/config/mediamanagement")
async def config_mediamanagement():
    return {"id": 1, "autoUnmonitorPreviouslyDownloadedTracks": False, "recycleBin": "", "downloadPropersAndRepacks": "preferAndUpgrade", "createEmptyArtistFolders": False, "deleteEmptyFolders": False, "fileDate": "none", "watchLibraryForChanges": False, "rescanAfterRefresh": "always", "allowFingerprinting": "never", "setPermissionsLinux": False, "chmodFolder": "755", "chownGroup": "", "skipFreeSpaceCheckWhenImporting": False, "minimumFreeSpaceWhenImporting": 100, "copyUsingHardlinks": True, "importExtraFiles": False, "extraFileExtensions": ""}


@router.get("/config/naming")
async def config_naming():
    return {"id": 1, "renameTracks": False, "replaceIllegalCharacters": True, "colonReplacementFormat": 0, "standardTrackFormat": "{track:00} - {Track Title}", "multiDiscTrackFormat": "{medium:0}-{track:00} - {Track Title}", "artistFolderFormat": settings.artist_folder_format.replace("{artist}", "{Artist Name}"), "albumFolderFormat": settings.album_folder_format.replace("{album}", "{Album Title}").replace("{year}", "{Release Year}"), "includeArtistName": False, "includeAlbumTitle": False, "includeQuality": False, "replaceSpaces": False}


@router.get("/config/ui")
async def config_ui():
    return {"id": 1, "firstDayOfWeek": 1, "calendarWeekColumnHeader": "ddd M/D", "shortDateFormat": "MMM D YYYY", "longDateFormat": "dddd, MMMM D YYYY", "timeFormat": "HH:mm", "showRelativeDates": True, "enableColorImpairedMode": False, "uiLanguage": 1, "theme": "dark"}


@router.get("/language")
async def languages():
    return [{"id": 1, "name": "English", "nameLower": "english"}]


@router.get("/system/task")
async def system_tasks():
    st = scheduler.status()
    out = []
    for key, name, hours in (("refresh", "Refresh Artist", settings.refresh_interval_hours), ("scan", "Rescan Folders", settings.scan_interval_hours), ("recommendations", "Refresh Recommendations", settings.recommendations_interval_hours), ("notify", "Send Notifications", 0.25)):
        last = st["last"].get(key)
        out.append({"id": len(out) + 1, "name": name, "taskName": key, "interval": int(hours * 60), "lastExecution": last, "lastStartTime": last, "nextExecution": (datetime.fromisoformat(last) + timedelta(hours=hours)).isoformat() if last else None, "lastDuration": "00:00:00"})
    return out


@router.get("/update")
async def update_list():
    from chordarr.services import updates

    return await updates.lidarr_shape(__version__)


# --- backups ----------------------------------------------------------------------------


@router.get("/system/backup")
async def backups():
    from chordarr.services import backup

    return backup.list_backups()


@router.post("/system/backup", status_code=201)
async def backup_now():
    from chordarr.services import backup

    path = await backup.create("manual")
    return {"name": path.name, "path": f"/backup/manual/{path.name}"}


@router.delete("/system/backup/{backup_id}", status_code=204)
async def backup_delete(backup_id: int):
    from chordarr.services import backup

    for b in backup.list_backups():
        if b["id"] == backup_id:
            p = backup.find(b["type"], b["name"])
            if p:
                p.unlink()
            return
    raise HTTPException(404)
