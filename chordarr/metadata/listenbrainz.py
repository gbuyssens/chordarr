"""ListenBrainz Labs: similar artists keyed by MusicBrainz ids. Open data, no key."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from chordarr.config import settings
from chordarr.metadata.http import Client

log = logging.getLogger(__name__)
ALGORITHM = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
_client: Client | None = None


def client() -> Client:
    global _client
    if _client is None:
        _client = Client(settings.listenbrainz_labs_url, rate=4.0)
    return _client


@dataclass
class SimilarArtist:
    mbid: str
    name: str
    score: float


async def similar_artists(mbid: str, limit: int = 25) -> list[SimilarArtist]:
    try:
        data = await client().get_json("similar-artists/json", {"artist_mbids": mbid, "algorithm": ALGORITHM})
    except Exception as exc:
        log.warning("listenbrainz similar %s: %s", mbid, exc)
        return []
    out = []
    for row in data[:limit] if isinstance(data, list) else []:
        out.append(SimilarArtist(mbid=row.get("artist_mbid", ""), name=row.get("name", ""), score=float(row.get("score", 0))))
    return out
