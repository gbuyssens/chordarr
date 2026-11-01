"""Import tracked artists from an existing Lidarr instance.

Two paths: the SQLite database (``lidarr.db``, read-only) or the Lidarr API
(``/api/v1/artist``). Both give the MusicBrainz artist id, the folder and the monitored
flag; nothing else is needed, Chordarr rebuilds the catalogue itself.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx

from chordarr.config import settings

log = logging.getLogger(__name__)


@dataclass
class LidarrArtist:
    mbid: str
    name: str
    path: str | None
    monitored: bool


def read_database(db_path: str | Path) -> list[LidarrArtist]:
    uri = f"file:{Path(db_path).resolve()}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = con.execute(
            """
            SELECT m.ForeignArtistId, m.Name, a.Path, a.Monitored
            FROM Artists a JOIN ArtistMetadata m ON a.ArtistMetadataId = m.Id
            ORDER BY m.Name
            """
        ).fetchall()
    finally:
        con.close()
    return [LidarrArtist(mbid=r[0], name=r[1], path=r[2], monitored=bool(r[3])) for r in rows]


async def read_api(url: str, api_key: str) -> list[LidarrArtist]:
    async with httpx.AsyncClient(timeout=30, headers={"X-Api-Key": api_key, "User-Agent": settings.user_agent}) as c:
        r = await c.get(url.rstrip("/") + "/api/v1/artist")
        r.raise_for_status()
    return [
        LidarrArtist(mbid=a["foreignArtistId"], name=a["artistName"], path=a.get("path"), monitored=bool(a.get("monitored", True)))
        for a in r.json()
    ]
