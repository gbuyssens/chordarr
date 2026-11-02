"""Adding and refreshing tracked artists."""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chordarr.metadata import deezer
from chordarr.metadata import musicbrainz as mb
from chordarr.metadata.coverart import release_group_cover
from chordarr.models import Appearance, Artist, Release, utcnow
from chordarr.services.events import add_event

log = logging.getLogger(__name__)


class ArtistExists(Exception):
    pass


MONITOR_MODES = ("all", "future", "missing", "existing", "first", "latest", "none")


async def add_artist(
    session: AsyncSession,
    mbid: str,
    path: str | None = None,
    monitored: bool = True,
    monitor_types: list[str] | None = None,
    monitor_appearances: bool = True,
    monitor: str = "all",
    monitor_new_items: str = "all",
    refresh: bool = True,
) -> Artist:
    """Track a new artist.

    ``monitor`` is Lidarr's add option: which of the artist's own releases (of the chosen
    types) start monitored. ``all``, ``future`` (release date after today), ``missing``
    (not on disk), ``existing`` (on disk), ``first`` and ``latest`` (one album), ``none``.
    It is applied once, after the first catalogue fetch and library scan; afterwards
    ``monitor_new_items`` decides for releases that appear later.
    """
    existing = await session.scalar(select(Artist).where(Artist.mbid == mbid))
    if existing:
        raise ArtistExists(existing.name)
    info = await mb.get_artist(mbid)
    if not info:
        raise ValueError(f"Unknown MusicBrainz artist {mbid}")
    artist = Artist(
        mbid=info.mbid,
        name=info.name,
        sort_name=info.sort_name or info.name,
        disambiguation=info.disambiguation,
        type=info.type,
        country=info.country,
        deezer_id=info.deezer_id,
        path=path,
        monitored=monitored,
        monitor_types=monitor_types or ["Album", "EP", "Single"],
        monitor_appearances=monitor_appearances,
        monitor_new_items=monitor_new_items if monitor_new_items in ("all", "none") else "all",
    )
    session.add(artist)
    await session.flush()
    add_event(session, "artist_added", f"{artist.name} added", artist=artist)
    # Commit now: the catalogue fetch that follows takes up to minutes of MusicBrainz calls,
    # and the SQLite write lock must not be held meanwhile (every other writer would wait).
    await session.commit()
    if refresh:
        await refresh_artist(session, artist, announce=False)
        await session.commit()
        if monitor != "all":
            from chordarr.services import library

            await session.refresh(artist, ["releases"])
            await library.scan_artist(session, artist)
            await apply_monitor_mode(session, artist, monitor)
            await session.commit()
    # Reload collections so callers can read artist.releases without lazy IO.
    await session.refresh(artist, ["releases", "appearances"])
    return artist


async def apply_monitor_mode(session: AsyncSession, artist: Artist, mode: str) -> None:
    """Set the monitored flag of the artist's own releases according to a Lidarr add option."""
    if mode not in MONITOR_MODES:
        raise ValueError(f"unknown monitor option {mode!r}")
    rows = (await session.scalars(select(Release).where(Release.artist_id == artist.id, Release.kind == "own"))).all()
    eligible = [r for r in rows if r.primary_type in (artist.monitor_types or []) and not r.secondary_types]
    today = date.today()
    chosen: set[int] = set()
    if mode == "all":
        chosen = {r.id for r in eligible}
    elif mode == "future":
        chosen = {r.id for r in eligible if r.first_release_date and r.first_release_date > today}
    elif mode == "missing":
        chosen = {r.id for r in eligible if r.status != "have"}
    elif mode == "existing":
        chosen = {r.id for r in eligible if r.status == "have"}
    elif mode in ("first", "latest"):
        albums = sorted((r for r in eligible if r.primary_type == "Album" and r.first_release_date), key=lambda r: r.first_release_date)
        if albums:
            chosen = {(albums[0] if mode == "first" else albums[-1]).id}
    for r in rows:
        r.monitored = r.id in chosen
    await session.flush()


def _role_for(track: mb.TrackCredit, artist_mbid: str, group: mb.ReleaseGroupInfo) -> str:
    """Guess how the artist appears on a track of a release it does not own."""
    credit = track.artist_credit.casefold()
    if {"Compilation", "DJ-mix"} & set(group.secondary_types) or "Various Artists" in group.artist_credit:
        return "compilation"
    if "remix" in track.title.casefold():
        return "remixer"
    if len(track.artist_mbids) > 1 and ("feat" in credit or " & " in credit or " and " in credit or " with " in credit or ", " in credit):
        return "featured"
    return "credited"


async def refresh_artist(session: AsyncSession, artist: Artist, announce: bool = True) -> dict:
    """Pull the artist's release groups and appearances from MusicBrainz, diff and record.

    Network first, database second: every MusicBrainz and Deezer call happens before the
    first write, so the SQLite write lock is held for milliseconds, not for the thirty
    seconds a prolific artist takes to fetch. ``announce`` controls whether differences
    become events (and notifications): the first fetch of a new artist is not news.
    """
    counters = {"own_new": 0, "appearance_new": 0, "updated": 0}
    # The first fetch of an artist (added without refresh, e.g. a Lidarr import) is a
    # catalogue load, not news: never announce it, whatever the caller asked.
    announce = announce and artist.last_refresh is not None
    try:
        info = await mb.get_artist(artist.mbid)
        picture = await _fetch_picture(artist, info)
        own_groups = await mb.artist_release_groups(artist.mbid)
        appearances = await mb.artist_appearances(artist.mbid)
    except Exception as exc:
        log.exception("refresh %s failed", artist.name)
        artist.refresh_error = str(exc)[:500]
        add_event(session, "error", f"Refresh failed for {artist.name}", str(exc)[:500], artist=artist)
        await session.flush()
        return counters

    if info:
        artist.name, artist.sort_name = info.name, info.sort_name or info.name
        artist.disambiguation, artist.type, artist.country = info.disambiguation, info.type, info.country
        if info.deezer_id and not artist.deezer_id:
            artist.deezer_id = info.deezer_id
    if picture:
        artist.deezer_id, artist.image_url = picture

    # Explicit queries: the async session cannot lazy-load collections of an object that
    # was just flushed, and selectin loading does not cover that case either.
    existing: dict[str, Release] = {
        r.mbid: r for r in (await session.scalars(select(Release).where(Release.artist_id == artist.id))).all()
    }
    known_appearances: set[tuple[str, str]] = set(
        (
            await session.execute(
                select(Release.mbid, Appearance.recording_mbid)
                .join(Appearance, Appearance.release_id == Release.id)
                .where(Appearance.artist_id == artist.id)
            )
        ).all()
    )

    # 1. Release groups the artist is credited on.
    own_ids = {rg.mbid for rg in own_groups}
    for rg in own_groups:
        rel = existing.get(rg.mbid)
        if rel is None:
            # Studio releases of the chosen types are wanted; live albums, compilations,
            # remixes and soundtracks (MusicBrainz secondary types) are listed, not wanted.
            rel = Release(
                artist_id=artist.id,
                mbid=rg.mbid,
                kind="own",
                monitored=artist.monitored
                and rg.primary_type in (artist.monitor_types or [])
                and not rg.secondary_types
                and (announce is False or artist.monitor_new_items != "none"),
                cover_url=release_group_cover(rg.mbid),
            )
            session.add(rel)
            existing[rg.mbid] = rel
            counters["own_new"] += 1
            _apply_group(rel, rg)
            await session.flush()
            if announce:
                _announce_release(session, artist, rel)
        else:
            if _apply_group(rel, rg):
                counters["updated"] += 1
            if rel.kind != "own":
                rel.kind = "own"

    # 2. Releases where the artist is credited on tracks only → appearances.
    for app in appearances:
        rg = app.release_group
        if rg.mbid in own_ids:
            continue
        rel = existing.get(rg.mbid)
        is_new_release = rel is None
        if rel is None:
            rel = Release(
                artist_id=artist.id,
                mbid=rg.mbid,
                kind="appearance",
                monitored=artist.monitored and artist.monitor_appearances,
                cover_url=release_group_cover(rg.mbid),
            )
            session.add(rel)
            existing[rg.mbid] = rel
            _apply_group(rel, rg)
            await session.flush()
        roles = {_role_for(track, artist.mbid, rg) for track in app.tracks}
        if is_new_release:
            # Compilations and "best of" slots are noise for most people: present, but not
            # wanted by default. Real features and remixes follow the artist's setting.
            rel.monitored = artist.monitored and artist.monitor_appearances and roles != {"compilation"} and (announce is False or artist.monitor_new_items != "none")
        for track in app.tracks:
            key = (rg.mbid, track.recording_mbid)
            if key in known_appearances:
                continue
            ap = Appearance(
                artist_id=artist.id,
                release_id=rel.id,
                recording_mbid=track.recording_mbid,
                recording_title=track.title,
                artist_credit=track.artist_credit,
                role=_role_for(track, artist.mbid, rg),
            )
            session.add(ap)
            known_appearances.add(key)
            if is_new_release:
                counters["appearance_new"] += 1
                is_new_release = False
                if announce:
                    _announce_appearance(session, artist, rel, ap)

    artist.last_refresh = utcnow()
    artist.refresh_error = None
    await session.flush()
    return counters


def _apply_group(rel: Release, rg: mb.ReleaseGroupInfo) -> bool:
    changed = False
    for attr, value in (
        ("title", rg.title),
        ("artist_credit", rg.artist_credit),
        ("primary_type", rg.primary_type),
        ("secondary_types", rg.secondary_types),
        ("first_release_date", rg.first_release_date),
        ("date_precision", rg.date_precision),
    ):
        if getattr(rel, attr) != value:
            setattr(rel, attr, value)
            changed = True
    return changed


def _announce_release(session, artist: Artist, rel: Release) -> None:
    when = rel.first_release_date
    upcoming = when and when > date.today()
    kind = "release_upcoming" if upcoming else "release_found"
    verb = "announced" if upcoming else "found"
    add_event(
        session,
        kind,
        f"{artist.name}: {rel.title} ({rel.type_label}) {verb}",
        f"Release date: {when.isoformat() if when else 'unknown'}",
        artist=artist,
        release=rel,
    )


def _announce_appearance(session, artist: Artist, rel: Release, ap: Appearance) -> None:
    add_event(
        session,
        "appearance_found",
        f"{artist.name} appears on {rel.artist_credit or '?'} – {rel.title}",
        f"Track: {ap.recording_title} ({ap.artist_credit})",
        artist=artist,
        release=rel,
        recording={"mbid": ap.recording_mbid, "title": ap.recording_title, "role": ap.role},
    )


async def _fetch_picture(artist: Artist, info: mb.ArtistInfo | None) -> tuple[int, str | None] | None:
    """Deezer id and picture URL, or None when nothing needs changing."""
    if artist.image_url and artist.deezer_id:
        return None
    deezer_id = artist.deezer_id or (info.deezer_id if info else None)
    dz = await deezer.get_artist(deezer_id) if deezer_id else await deezer.search_artist(artist.name)
    if not dz:
        return None
    return dz.id, dz.picture or artist.image_url
