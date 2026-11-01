"""Artist recommendations built from what is already tracked.

Each tracked artist votes for its neighbours: Deezer "related artists" and ListenBrainz
"similar artists". A candidate recommended by several tracked artists climbs. Candidates
already tracked or dismissed are skipped. ListenBrainz candidates carry a MusicBrainz id
and can be added in one click; Deezer-only candidates are resolved on MusicBrainz when
the user adds them.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chordarr.metadata import deezer, listenbrainz
from chordarr.models import Artist, Recommendation, utcnow

log = logging.getLogger(__name__)


def _key(name: str) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum())


async def rebuild(session: AsyncSession, max_per_artist: int = 20) -> int:
    artists = (await session.scalars(select(Artist))).all()
    tracked_mbids = {a.mbid for a in artists}
    tracked_names = {_key(a.name) for a in artists}

    candidates: dict[str, dict] = {}
    votes: dict[str, float] = defaultdict(float)
    reasons: dict[str, list] = defaultdict(list)

    for artist in artists:
        if not artist.monitored:
            continue
        for sim in await listenbrainz.similar_artists(artist.mbid, limit=max_per_artist):
            if not sim.mbid or sim.mbid in tracked_mbids:
                continue
            k = _key(sim.name)
            c = candidates.setdefault(k, {"name": sim.name, "mbid": sim.mbid, "deezer_id": None, "image_url": None})
            c["mbid"] = c["mbid"] or sim.mbid
            votes[k] += 1.0 + min(sim.score, 5000) / 5000
            reasons[k].append({"artist": artist.name, "source": "listenbrainz"})
        if artist.deezer_id:
            for i, rel in enumerate(await deezer.related_artists(artist.deezer_id, limit=max_per_artist)):
                k = _key(rel.name)
                if k in tracked_names:
                    continue
                c = candidates.setdefault(k, {"name": rel.name, "mbid": None, "deezer_id": None, "image_url": None})
                c["deezer_id"] = c["deezer_id"] or rel.id
                c["image_url"] = c["image_url"] or rel.picture
                votes[k] += 1.0 + (max_per_artist - i) / max_per_artist
                reasons[k].append({"artist": artist.name, "source": "deezer"})

    existing = {(_key(r.name)): r for r in (await session.scalars(select(Recommendation))).all()}
    kept = 0
    for k, c in candidates.items():
        if k in tracked_names:
            continue
        rec = existing.get(k)
        if rec is None:
            rec = Recommendation(name=c["name"])
            session.add(rec)
        if rec.dismissed:
            continue
        rec.mbid = rec.mbid or c["mbid"]
        rec.deezer_id = rec.deezer_id or c["deezer_id"]
        rec.image_url = rec.image_url or c["image_url"]
        rec.score = round(votes[k], 2)
        # De-duplicate reasons, keep order.
        seen = set()
        rec.reasons = [r for r in reasons[k] if not ((r["artist"], r["source"]) in seen or seen.add((r["artist"], r["source"])))]
        rec.updated_at = utcnow()
        kept += 1
    # Drop stale non-dismissed recommendations that no longer appear.
    for k, rec in existing.items():
        if k not in candidates and not rec.dismissed:
            await session.delete(rec)
    await session.flush()
    return kept
