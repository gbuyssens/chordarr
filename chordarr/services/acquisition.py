"""Finding and grabbing releases: search, candidate selection, hand-over to a download client.

Candidate selection is deliberately simple. A candidate must name the artist and the
release, carry a quality the profile accepts (``lossless`` > ``high`` > ``low``/``unknown``)
and, for torrents, enough seeders. Among the accepted ones the best quality wins, then
seeders, then indexer priority. Nothing is grabbed automatically unless ``auto_grab`` is on.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from functools import lru_cache

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chordarr.config import settings
from chordarr.models import Artist, Download, DownloadClient, Indexer, Release, utcnow
from chordarr.services import direct, downloadclients, indexers
from chordarr.services.events import add_event
from chordarr.services.indexers import Candidate
from chordarr.services.library import normalise
from chordarr.services.prefs import get_prefs

log = logging.getLogger(__name__)

QUALITY_RANK = {"lossless": 3, "high": 2, "low": 1, "unknown": 1}
PROFILE_ACCEPTS = {
    "lossless": {"lossless"},
    "high": {"lossless", "high"},
    "any": {"lossless", "high", "low", "unknown"},
}


class GrabError(Exception):
    pass


@lru_cache(maxsize=8192)
def _tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in normalise(text).split() if t)


def tokens(text: str) -> set[str]:
    return set(_tokens(text))


# Words a release title may carry on top of the artist and album: formats, editions, sources.
NOISE_WORDS = frozenset(
    ["flac", "alac", "mp3", "aac", "m4a", "ogg", "opus", "wav", "ape", "wv", "aiff", "dsd", "dsf", "mqa", "lossless", "lossy", "web", "webdl", "webrip", "cd", "cdrip", "vinyl", "lp", "ep", "single", "album", "ost", "soundtrack", "deluxe", "edition", "version", "remaster", "remastered", "expanded", "extended", "bonus", "tracks", "anniversary", "collector", "collectors", "explicit", "clean", "reissue", "special", "limited", "digital", "hi", "res", "hires", "hd", "bit", "bits", "bitrate", "khz", "kbps", "kb", "cbr", "vbr", "stereo", "mono", "japan", "japanese", "jp", "us", "uk", "fr", "eu", "de", "import", "retail", "proper", "repack", "read", "nfo", "complete", "discography", "the", "and", "feat", "ft", "vs", "x", "qobuz", "deezer", "tidal", "soundcloud", "amazon", "streamrip"]
)


def extra_words(candidate_title: str, artist_name: str, title: str) -> list[str]:
    """Words of the candidate that belong neither to the artist, nor to the title, nor to the
    usual noise (formats, editions, years, bit depths); the last word is tolerated as a
    release-group tag. Any such word means the candidate names something else:
    "Damso QALF infinity 2021 FLAC" is not "QALF"."""
    words = [w for w in normalise(candidate_title).split() if w]
    known = _tokens(artist_name) | _tokens(title)
    out = []
    for i, w in enumerate(words):
        if w in known or w in NOISE_WORDS or any(ch.isdigit() for ch in w):
            continue
        if i == len(words) - 1:
            continue  # scene group tag at the end
        out.append(w)
    return out


def matches_release(candidate: Candidate, artist_name: str, title: str, other_titles: tuple[str, ...] = ()) -> bool:
    """Every word of the artist and of the release title appears in the candidate title, the
    candidate adds no word that is not noise, and it does not name another release of the
    artist whose title extends this one ("QALF infinity" is not a match for "QALF")."""
    have = _tokens(candidate.title)
    want = _tokens(artist_name) | _tokens(title)
    if not want or not want <= have:
        return False
    if extra_words(candidate.title, artist_name, title):
        return False
    mine = _tokens(title)
    for other in other_titles:
        o = _tokens(other)
        if o != mine and mine < o and o <= have:
            return False
    return True


async def sibling_titles(session: AsyncSession, release: Release) -> tuple[str, ...]:
    rows = (await session.scalars(select(Release.title).where(Release.artist_id == release.artist_id, Release.kind == "own", Release.id != release.id))).all()
    return tuple(rows)


def evaluate(
    candidates: list[Candidate],
    artist_name: str,
    release: Release,
    profile: str,
    indexers_by_id: dict[int, Indexer],
    active_guids: set[str] | None = None,
    other_titles: tuple[str, ...] = (),
) -> list[Candidate]:
    """Fill ``rejections`` and ``score`` on each candidate and return them best first."""
    accepts = PROFILE_ACCEPTS.get(profile, PROFILE_ACCEPTS["lossless"])
    for c in candidates:
        c.rejections = []
        if not matches_release(c, artist_name, release.title, other_titles):
            c.rejections.append("title does not name this artist and release (or names another release of theirs)")
        if c.quality not in accepts:
            c.rejections.append(f"quality {c.quality} below the {profile} profile")
        idx = indexers_by_id.get(c.indexer_id)
        if c.protocol == "torrent" and idx and idx.minimum_seeders and c.seeders is not None and c.seeders < idx.minimum_seeders:
            c.rejections.append(f"{c.seeders} seeders (minimum {idx.minimum_seeders})")
        if active_guids and c.guid in active_guids:
            c.rejections.append("already in the queue")
        extra_words = len(tokens(c.title) - tokens(artist_name) - tokens(release.title))
        c.score = (
            QUALITY_RANK.get(c.quality, 0) * 10_000
            + (2_000 if c.protocol == "direct" else 0)  # no seeding, no indexer luck: preferred at equal quality
            + min(c.seeders or 0, 200) * 20
            + (50 - min(idx.priority if idx else 25, 50)) * 10
            - min(extra_words, 20)
        )
    return sorted(candidates, key=lambda c: (c.accepted, c.score), reverse=True)


async def _usable_indexers(session: AsyncSession, interactive: bool) -> list[Indexer]:
    rows = (await session.scalars(select(Indexer).order_by(Indexer.priority, Indexer.id))).all()
    flag = "enable_interactive_search" if interactive else "enable_automatic_search"
    return [i for i in rows if getattr(i, flag)]


async def _direct_clients(session: AsyncSession) -> list[DownloadClient]:
    rows = (await session.scalars(select(DownloadClient).where(DownloadClient.enabled.is_(True)).order_by(DownloadClient.priority, DownloadClient.id))).all()
    return [c for c in rows if c.protocol == "direct"]


async def search_release(session: AsyncSession, release: Release, interactive: bool = True) -> list[Candidate]:
    """Query every usable indexer and direct source at once, ranked for this release."""
    artist = await session.get(Artist, release.artist_id)
    idx = await _usable_indexers(session, interactive)
    directs = await _direct_clients(session)
    if not artist or (not idx and not directs):
        return []
    prefs = await get_prefs(session)
    results = await asyncio.gather(
        *(indexers.search(i, artist.name, release.title, release.mbid) for i in idx),
        *(direct.search(c, artist.name, release.title) for c in directs),
        return_exceptions=True,
    )
    candidates: list[Candidate] = []
    direct_results, results = results[len(idx) :], results[: len(idx)]
    for client, res in zip(directs, direct_results, strict=True):
        if isinstance(res, BaseException):
            client.last_error = f"{utcnow():%Y-%m-%d %H:%M} {res}"[:500]
            log.warning("direct search on %s failed: %s", client.name, res)
            continue
        client.last_error = None
        candidates.extend(res)
    for indexer, res in zip(idx, results, strict=True):
        if isinstance(res, BaseException):
            indexer.last_error = f"{utcnow():%Y-%m-%d %H:%M} {res}"[:500]
            log.warning("search on %s failed: %s", indexer.name, res)
            continue
        indexer.last_error = None
        candidates.extend(res)
    active = set(
        (await session.scalars(select(Download.guid).where(Download.status.in_(("grabbed", "downloading", "completed", "importing"))))).all()
    )
    return evaluate(candidates, artist.name, release, prefs.quality_profile, {i.id: i for i in idx}, active, await sibling_titles(session, release))


async def fetch_payload(url: str) -> tuple[bytes | None, str | None]:
    """Download what the indexer links to: (file bytes, None) or (None, magnet link)."""
    if url.startswith("magnet:"):
        return None, url
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": settings.user_agent}, follow_redirects=False) as client:
        for _ in range(5):
            r = await client.get(url)
            if r.status_code in (301, 302, 303, 307, 308):
                target = r.headers.get("Location", "")
                if target.startswith("magnet:"):
                    return None, target
                url = str(httpx.URL(url).join(target))
                continue
            r.raise_for_status()
            return r.content, None
    raise GrabError("too many redirects")


async def pick_client(session: AsyncSession, protocol: str, preferred_id: int | None = None) -> DownloadClient:
    rows = (await session.scalars(select(DownloadClient).where(DownloadClient.enabled.is_(True)).order_by(DownloadClient.priority, DownloadClient.id))).all()
    rows = [c for c in rows if c.protocol == protocol]
    if preferred_id:
        rows = [c for c in rows if c.id == preferred_id] or rows
    if not rows:
        raise GrabError(f"no enabled {protocol} download client")
    return rows[0]


async def grab(session: AsyncSession, release: Release, candidate: Candidate, manual: bool = False) -> Download:
    """Send one candidate to a download client and put it in the queue."""
    artist = await session.get(Artist, release.artist_id)
    if candidate.protocol == "direct":
        client_id, _source, _album = direct.parse_ref(candidate.download_url)
        client_cfg = await session.get(DownloadClient, client_id)
        if client_cfg is None or not client_cfg.enabled:
            raise GrabError("the direct source for this result is gone")
        payload, link = None, candidate.download_url
    else:
        indexer = await session.get(Indexer, candidate.indexer_id)
        client_cfg = await pick_client(session, candidate.protocol, indexer.download_client_id if indexer else None)
        payload, link = await fetch_payload(candidate.download_url)
    if payload is not None and candidate.protocol == "torrent":
        try:
            downloadclients.torrent_info(payload)
        except ValueError as exc:
            raise GrabError(f"the indexer did not return a torrent file ({exc})") from exc
    if payload is not None and candidate.protocol == "usenet" and b"<nzb" not in payload[:2000]:
        raise GrabError("the indexer did not return an NZB file")
    try:
        async with downloadclients.make_client(client_cfg) as dc:
            kw = {"torrent": payload} if candidate.protocol == "torrent" else ({"nzb": payload} if candidate.protocol == "usenet" else {})
            client_id = await dc.add(candidate.title, url=link, **kw)
        client_cfg.last_error = None
    except Exception as exc:
        client_cfg.last_error = f"{utcnow():%Y-%m-%d %H:%M} {exc}"[:500]
        raise GrabError(f"{client_cfg.name}: {exc}") from exc

    dl = Download(
        release_id=release.id,
        indexer_id=candidate.indexer_id or None,  # direct sources have no indexer row
        download_client_id=client_cfg.id,
        title=candidate.title,
        guid=candidate.guid,
        download_url=candidate.download_url,
        protocol=candidate.protocol,
        size=candidate.size,
        seeders=candidate.seeders,
        quality=candidate.quality,
        client_id=client_id or None,
        status="grabbed",
        message="manual grab" if manual else "automatic grab",
    )
    session.add(dl)
    add_event(
        session,
        "release_grabbed",
        f"{artist.name if artist else release.artist_credit}: {release.title} sent to {client_cfg.name}",
        f"{candidate.title} · {candidate.quality} · {candidate.size_label} · {candidate.indexer_name}",
        artist=artist,
        release=release,
        download={"title": candidate.title, "indexer": candidate.indexer_name, "quality": candidate.quality, "size": candidate.size, "client": client_cfg.name},
    )
    await session.flush()
    return dl


async def wanted_releases(session: AsyncSession, due_only: bool = True, limit: int = 25) -> list[Release]:
    """Monitored, missing, already out, not in the queue and (optionally) not searched lately."""
    active = select(Download.release_id).where(Download.status.in_(("grabbed", "downloading", "completed", "importing")))
    q = (
        select(Release)
        .where(Release.monitored.is_(True), Release.status.in_(("missing", "partial")), Release.first_release_date <= date.today())
        .where(Release.id.not_in(active))
    )
    if due_only:
        cutoff = utcnow() - timedelta(hours=settings.search_interval_hours)
        q = q.where((Release.last_search_at.is_(None)) | (Release.last_search_at < cutoff))
    return (await session.scalars(q.order_by(Release.last_search_at.nulls_first(), Release.first_release_date.desc()).limit(limit))).all()


async def grab_best(session: AsyncSession, release: Release) -> Download | None:
    """Automatic search for one release: search everything, grab the best accepted result."""
    candidates = await search_release(session, release, interactive=False)
    release.last_search_at = utcnow()
    best = next((c for c in candidates if c.accepted), None)
    if best is None:
        return None
    return await grab(session, release, best)


async def search_wanted(session: AsyncSession, grab_all: bool = False) -> dict:
    """Automatic search for wanted releases; grabs the best candidate when auto_grab is on
    (or when ``grab_all`` is set by a user action such as *Search All*)."""
    counters = {"searched": 0, "found": 0, "grabbed": 0}
    prefs = await get_prefs(session)
    if not await _usable_indexers(session, interactive=False) and not await _direct_clients(session):
        return counters
    for release in await wanted_releases(session):
        counters["searched"] += 1
        candidates = await search_release(session, release, interactive=False)
        release.last_search_at = utcnow()
        best = next((c for c in candidates if c.accepted), None)
        if best is None:
            continue
        counters["found"] += 1
        if prefs.auto_grab or grab_all:
            try:
                await grab(session, release, best)
                counters["grabbed"] += 1
            except GrabError as exc:
                log.warning("grab failed for %s: %s", release.title, exc)
                add_event(session, "download_failed", f"Could not grab {release.title}", str(exc)[:400], release=release)
        await session.flush()
    return counters


async def rss_sync(session: AsyncSession) -> dict:
    """Read every RSS-enabled indexer once and grab wanted releases that just appeared."""
    counters = {"indexers": 0, "items": 0, "matched": 0, "grabbed": 0}
    prefs = await get_prefs(session)
    idx = [i for i in (await session.scalars(select(Indexer))).all() if i.enable_rss]
    if not idx:
        return counters
    wanted = await wanted_releases(session, due_only=False, limit=2000)
    if not wanted:
        return counters
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    titles_by_artist: dict[int, list[str]] = {}
    for r in (await session.scalars(select(Release).where(Release.kind == "own"))).all():
        titles_by_artist.setdefault(r.artist_id, []).append(r.title)
    results = await asyncio.gather(*(indexers.rss(i) for i in idx), return_exceptions=True)
    items: list[Candidate] = []
    for indexer, res in zip(idx, results, strict=True):
        counters["indexers"] += 1
        indexer.last_rss = utcnow()
        if isinstance(res, BaseException):
            indexer.last_error = f"{utcnow():%Y-%m-%d %H:%M} {res}"[:500]
            log.warning("rss on %s failed: %s", indexer.name, res)
            continue
        indexer.last_error = None
        items.extend(res)
    counters["items"] = len(items)
    by_id = {i.id: i for i in idx}
    for release in wanted:
        artist = artists.get(release.artist_id)
        if not artist:
            continue
        others = tuple(t for t in titles_by_artist.get(release.artist_id, []) if t != release.title)
        hits = [c for c in items if matches_release(c, artist.name, release.title, others)]
        if not hits:
            continue
        ranked = evaluate(hits, artist.name, release, prefs.quality_profile, by_id, other_titles=others)
        best = next((c for c in ranked if c.accepted), None)
        if best is None:
            continue
        counters["matched"] += 1
        if prefs.auto_grab:
            try:
                await grab(session, release, best)
                counters["grabbed"] += 1
            except GrabError as exc:
                log.warning("rss grab failed for %s: %s", release.title, exc)
    await session.flush()
    return counters
