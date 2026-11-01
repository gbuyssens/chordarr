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


async def add_artist(
    session: AsyncSession,
    mbid: str,
    path: str | None = None,
    monitored: bool = True,
    monitor_types: list[str] | None = None,
    monitor_appearances: bool = True,
    refresh: bool = True,
) -> Artist:
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
    )
    session.add(artist)
    await session.flush()
    add_event(session, "artist_added", f"{artist.name} added", artist=artist)
    if refresh:
        await refresh_artist(session, artist, announce=False)
    # Reload collections so callers can read artist.releases without lazy IO.
    await session.refresh(artist, ["releases", "appearances"])
    return artist


def _role_for(track: mb.TrackCredit, artist_mbid: str, group: mb.ReleaseGroupInfo) -> str:
    """Guess how the artist appears on a track of a release it does not own."""
    credit = track.artist_credit.casefold()
    if "remix" in track.title.casefold():
        return "remixer"
    if len(track.artist_mbids) > 1 and ("feat" in credit or " & " in credit or " and " in credit or " with " in credit or ", " in credit):
        return "featured"
    if "Compilation" in group.secondary_types or "Various Artists" in group.artist_credit:
        return "compilation"
    return "credited"


async def refresh_artist(session: AsyncSession, artist: Artist, announce: bool = True) -> dict:
    """Pull the artist's release groups and appearances from MusicBrainz, diff and record.

    Returns counters for the caller's logs. ``announce`` controls whether differences are
    recorded as events (and therefore notified): the very first fetch of a new artist is
    not a stream of "new release" news.
    """
    counters = {"own_new": 0, "appearance_new": 0, "updated": 0}
    try:
        info = await mb.get_artist(artist.mbid)
        if info:
            artist.name, artist.sort_name = info.name, info.sort_name or info.name
            artist.disambiguation, artist.type, artist.country = info.disambiguation, info.type, info.country
            if info.deezer_id and not artist.deezer_id:
                artist.deezer_id = info.deezer_id
        await _refresh_picture(artist)

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
        own_groups = await mb.artist_release_groups(artist.mbid)
        own_ids = {rg.mbid for rg in own_groups}
        for rg in own_groups:
            rel = existing.get(rg.mbid)
            if rel is None:
                rel = Release(
                    artist_id=artist.id,
                    mbid=rg.mbid,
                    kind="own",
                    monitored=artist.monitored and rg.primary_type in (artist.monitor_types or []),
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
        for app in await mb.artist_appearances(artist.mbid):
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
    except Exception as exc:
        log.exception("refresh %s failed", artist.name)
        message = str(exc)[:500]
        try:
            await session.flush()
        except Exception:
            # The flush itself failed: drop the half-applied changes, keep the error on record.
            await session.rollback()
            artist = await session.get(Artist, artist.id) or artist
        artist.refresh_error = message
        add_event(session, "error", f"Refresh failed for {artist.name}", message, artist=artist)
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


async def _refresh_picture(artist: Artist) -> None:
    if artist.image_url and artist.deezer_id:
        return
    dz = await deezer.get_artist(artist.deezer_id) if artist.deezer_id else await deezer.search_artist(artist.name)
    if dz:
        artist.deezer_id = dz.id
        artist.image_url = dz.picture or artist.image_url
