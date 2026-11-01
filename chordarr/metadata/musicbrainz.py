"""MusicBrainz web service client.

MusicBrainz allows one request per second per IP for anonymous use. Everything here goes
through a single shared rate limiter so background refreshes and interactive searches
never trip the limit together.

Two browse calls carry the whole feature:

* ``release-group?artist=MBID`` lists every release group where the artist is part of the
  *release group* artist credit: their albums, EPs, singles, and split or collaborative
  releases.
* ``release?track_artist=MBID`` lists every release where the artist is credited on a
  *track* but not on the release itself. Features ("A feat. B") are modelled this way on
  MusicBrainz: the release is credited to A only, the track to A and B. Grouped by release
  group, those are the artist's **appearances**; the tracks tell where exactly.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from chordarr.config import settings
from chordarr.metadata.http import Client

log = logging.getLogger(__name__)

PRIMARY_TYPES = ("Album", "Single", "EP", "Broadcast", "Other")
_client: Client | None = None


def client() -> Client:
    global _client
    if _client is None:
        _client = Client(settings.musicbrainz_url, rate=1.0)
    return _client


@dataclass
class ArtistInfo:
    mbid: str
    name: str
    sort_name: str = ""
    disambiguation: str = ""
    type: str = ""
    country: str = ""
    score: int = 0
    deezer_id: int | None = None
    urls: dict[str, str] = field(default_factory=dict)


@dataclass
class ReleaseGroupInfo:
    mbid: str
    title: str
    primary_type: str
    secondary_types: list[str]
    first_release_date: date | None
    date_precision: str
    artist_credit: str
    artist_mbids: list[str]


@dataclass
class TrackCredit:
    recording_mbid: str
    title: str
    artist_credit: str
    artist_mbids: list[str]


@dataclass
class AppearanceInfo:
    release_group: ReleaseGroupInfo
    tracks: list[TrackCredit]


def parse_partial_date(value: str | None) -> tuple[date | None, str]:
    """MusicBrainz dates can be ``2024``, ``2024-05`` or ``2024-05-17``."""
    if not value:
        return None, "none"
    parts = value.split("-")
    try:
        if len(parts) == 1:
            return date(int(parts[0]), 1, 1), "year"
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 1), "month"
        return date(int(parts[0]), int(parts[1]), int(parts[2])), "day"
    except ValueError:
        return None, "none"


def credit_string(credits: list[dict] | None) -> str:
    if not credits:
        return ""
    return "".join((c.get("name") or c.get("artist", {}).get("name", "")) + (c.get("joinphrase") or "") for c in credits)


def credit_mbids(credits: list[dict] | None) -> list[str]:
    return [c["artist"]["id"] for c in credits or [] if c.get("artist", {}).get("id")]


def _release_group(rg: dict) -> ReleaseGroupInfo:
    d, precision = parse_partial_date(rg.get("first-release-date"))
    return ReleaseGroupInfo(
        mbid=rg["id"],
        title=rg.get("title", ""),
        primary_type=rg.get("primary-type") or "Other",
        secondary_types=list(rg.get("secondary-types") or []),
        first_release_date=d,
        date_precision=precision,
        artist_credit=credit_string(rg.get("artist-credit")),
        artist_mbids=credit_mbids(rg.get("artist-credit")),
    )


def _escape_lucene(term: str) -> str:
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", term)


async def search_artists(term: str, limit: int = 10) -> list[ArtistInfo]:
    term = term.strip()
    if not term:
        return []
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", term):
        info = await get_artist(term)
        return [info] if info else []
    data = await client().get_json("artist", {"query": f'artist:"{_escape_lucene(term)}"', "fmt": "json", "limit": limit})
    out = []
    for a in data.get("artists", []):
        out.append(
            ArtistInfo(
                mbid=a["id"],
                name=a.get("name", ""),
                sort_name=a.get("sort-name", ""),
                disambiguation=a.get("disambiguation", ""),
                type=a.get("type", "") or "",
                country=a.get("country", "") or "",
                score=int(a.get("score", 0)),
            )
        )
    return out


async def get_artist(mbid: str) -> ArtistInfo | None:
    try:
        a = await client().get_json(f"artist/{mbid}", {"inc": "url-rels", "fmt": "json"})
    except Exception as exc:  # 404 included
        log.warning("artist %s: %s", mbid, exc)
        return None
    urls: dict[str, str] = {}
    deezer_id = None
    for rel in a.get("relations", []):
        target = rel.get("url", {}).get("resource", "")
        if not target:
            continue
        urls[rel.get("type", "url")] = target
        m = re.search(r"deezer\.com/(?:[a-z]{2}/)?artist/(\d+)", target)
        if m:
            deezer_id = int(m.group(1))
    return ArtistInfo(
        mbid=a["id"],
        name=a.get("name", ""),
        sort_name=a.get("sort-name", ""),
        disambiguation=a.get("disambiguation", "") or "",
        type=a.get("type", "") or "",
        country=a.get("country", "") or "",
        deezer_id=deezer_id,
        urls=urls,
    )


async def _browse(entity: str, params: dict, key: str, count_key: str, max_items: int) -> list[dict]:
    items: list[dict] = []
    offset = 0
    while True:
        data = await client().get_json(entity, {**params, "fmt": "json", "limit": 100, "offset": offset})
        page = data.get(key, [])
        items.extend(page)
        total = int(data.get(count_key, len(items)))
        offset += len(page)
        if not page or offset >= total or offset >= max_items:
            break
    return items


async def artist_release_groups(mbid: str, max_items: int = 2000) -> list[ReleaseGroupInfo]:
    rows = await _browse(
        "release-group", {"artist": mbid, "inc": "artist-credits"}, "release-groups", "release-group-count", max_items
    )
    return [_release_group(rg) for rg in rows]


async def artist_appearances(mbid: str, max_items: int = 3000) -> list[AppearanceInfo]:
    """Release groups where the artist is credited on tracks but not on the release."""
    rows = await _browse(
        "release",
        {"track_artist": mbid, "inc": "release-groups+artist-credits+recordings"},
        "releases",
        "release-count",
        max_items,
    )
    groups: dict[str, AppearanceInfo] = {}
    for rel in rows:
        rg = rel.get("release-group")
        if not rg:
            continue
        rg = {**rg, "artist-credit": rg.get("artist-credit") or rel.get("artist-credit")}
        if not rg.get("first-release-date"):
            rg["first-release-date"] = rel.get("date")
        info = groups.get(rg["id"])
        if info is None:
            info = groups[rg["id"]] = AppearanceInfo(release_group=_release_group(rg), tracks=[])
        seen = {t.recording_mbid for t in info.tracks}
        for medium in rel.get("media", []) or []:
            for track in medium.get("tracks", []) or []:
                credits = track.get("artist-credit") or track.get("recording", {}).get("artist-credit") or []
                ids = credit_mbids(credits)
                if mbid not in ids:
                    continue
                rec_id = track.get("recording", {}).get("id") or track.get("id")
                if rec_id in seen:
                    continue
                seen.add(rec_id)
                info.tracks.append(
                    TrackCredit(recording_mbid=rec_id, title=track.get("title", ""), artist_credit=credit_string(credits), artist_mbids=ids)
                )
    return [g for g in groups.values() if g.tracks]


async def release_group_tracks(rg_mbid: str) -> int | None:
    """Track count of the earliest official release in the group, when known."""
    try:
        data = await client().get_json(
            "release", {"release-group": rg_mbid, "status": "official", "inc": "media", "fmt": "json", "limit": 25}
        )
    except Exception as exc:
        log.debug("release-group %s media: %s", rg_mbid, exc)
        return None
    best = None
    for rel in sorted(data.get("releases", []), key=lambda r: r.get("date") or "9999"):
        count = sum(int(m.get("track-count", 0)) for m in rel.get("media", []))
        if count:
            best = count
            break
    return best
