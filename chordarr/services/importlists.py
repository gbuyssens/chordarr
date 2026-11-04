"""Import lists: charts and feeds that add albums (and their artists) on a schedule.

Providers return (artist, album) pairs; the list adds the artist when needed, monitors
the listed album (or the whole artist), and asks for a search. Like Lidarr's Import Lists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chordarr.config import settings
from chordarr.metadata import musicbrainz as mb
from chordarr.models import Artist, ImportList, Release, utcnow
from chordarr.services import artists as artist_service
from chordarr.services.events import add_event
from chordarr.services.importer import same_album
from chordarr.services.library import normalise

log = logging.getLogger(__name__)

LIST_TYPES = {
    "apple_music_charts": {"label": "Apple Music Top Albums", "fields": ["country", "count"], "help": "Apple's public RSS charts: most played albums in a country (ISO code: fr, us, gb…), 10 to 100 entries."},
    "deezer_chart": {"label": "Deezer Chart Albums", "fields": ["count"], "help": "Deezer's public album chart (the country is the one Deezer sees for this server), up to 100 entries."},
}


@dataclass
class ListItem:
    artist: str
    album: str
    url: str = ""


async def fetch_items(lst: ImportList) -> list[ListItem]:
    cfg = lst.config or {}
    count = max(10, min(int(cfg.get("count") or 100), 100))
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": settings.user_agent}, follow_redirects=True) as c:
        if lst.type == "apple_music_charts":
            country = (cfg.get("country") or "fr").lower()
            r = await c.get(f"https://rss.marketingtools.apple.com/api/v2/{country}/music/most-played/{count}/albums.json")
            r.raise_for_status()
            return [ListItem(x.get("artistName", ""), x.get("name", ""), x.get("url", "")) for x in r.json()["feed"]["results"]]
        if lst.type == "deezer_chart":
            r = await c.get("https://api.deezer.com/chart/0/albums", params={"limit": count})
            r.raise_for_status()
            return [ListItem(x["artist"]["name"], x["title"], x.get("link", "")) for x in r.json().get("data", [])]
    raise ValueError(f"unknown list type {lst.type}")


def _clean_title(title: str) -> str:
    """Charts carry edition noise; keep what identifies the record."""
    return title.split(" - EP")[0].split(" - Single")[0].strip()


async def _find_release(session: AsyncSession, artist: Artist, album: str) -> Release | None:
    want = normalise(_clean_title(album))
    rows = (await session.scalars(select(Release).where(Release.artist_id == artist.id, Release.kind == "own"))).all()
    exact = [r for r in rows if normalise(r.title) == want]
    if exact:
        return sorted(exact, key=lambda r: (r.primary_type != "Album", r.first_release_date or utcnow().date()))[0]
    loose = [r for r in rows if same_album(normalise(r.title), want) or same_album(want, normalise(r.title))]
    if loose:
        return sorted(loose, key=lambda r: (r.primary_type != "Album", r.first_release_date or utcnow().date()))[0]
    return None


async def process(session: AsyncSession, lst: ImportList) -> dict:
    """Fetch the list, add what is missing, monitor the albums, return a summary and the release ids to search."""
    summary = {"items": 0, "artists_added": 0, "albums_monitored": 0, "already": 0, "not_found": [], "release_ids": []}
    items = await fetch_items(lst)
    summary["items"] = len(items)
    tracked = {a.mbid: a for a in (await session.scalars(select(Artist))).all()}
    by_name = {normalise(a.name): a for a in tracked.values()}
    whole_artist = lst.monitor == "artist"
    for item in items:
        if not item.artist or not item.album:
            continue
        artist = by_name.get(normalise(item.artist))
        if artist is None:
            found = await mb.search_artists(item.artist, limit=5)
            exact = [f for f in found if normalise(f.name) == normalise(item.artist)] or found[:1]
            if not exact:
                summary["not_found"].append(f"{item.artist} (artist)")
                continue
            mbid = exact[0].mbid
            artist = tracked.get(mbid)
            if artist is None:
                try:
                    artist = await artist_service.add_artist(
                        session,
                        mbid,
                        monitor="all" if whole_artist else "none",
                        monitor_new_items="all" if whole_artist else "none",
                        monitor_appearances=whole_artist,
                        with_appearances=whole_artist,
                    )
                except artist_service.ArtistExists:
                    artist = await session.scalar(select(Artist).where(Artist.mbid == mbid))
                except Exception as exc:
                    summary["not_found"].append(f"{item.artist} ({str(exc)[:40]})")
                    continue
                else:
                    summary["artists_added"] += 1
                    add_event(session, "artist_added", f"{artist.name} added by list {lst.name}", artist=artist)
                tracked[artist.mbid] = artist
                by_name[normalise(artist.name)] = artist
        if whole_artist:
            continue
        rel = await _find_release(session, artist, item.album)
        if rel is None:
            summary["not_found"].append(f"{item.artist} - {item.album}")
            continue
        if rel.status == "have":
            summary["already"] += 1
            continue
        if not rel.monitored:
            rel.monitored = True
            summary["albums_monitored"] += 1
        summary["release_ids"].append(rel.id)
        await session.commit()
    lst.last_run = utcnow()
    lst.last_error = None
    lst.last_summary = f"{summary['items']} items · {summary['artists_added']} artists added · {summary['albums_monitored']} albums monitored · {summary['already']} already on disk · {len(summary['not_found'])} not found"
    await session.commit()
    return summary


async def test(lst: ImportList) -> str:
    items = await fetch_items(lst)
    head = ", ".join(f"{i.artist} – {i.album}" for i in items[:3])
    return f"{len(items)} items ({head}…)"
