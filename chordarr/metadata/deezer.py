"""Deezer public API: artist pictures, related artists, release dates. No key needed."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from chordarr.config import settings
from chordarr.metadata.http import Client

log = logging.getLogger(__name__)
_client: Client | None = None


def client() -> Client:
    global _client
    if _client is None:
        _client = Client(settings.deezer_url, rate=8.0)
    return _client


@dataclass
class DeezerArtist:
    id: int
    name: str
    picture: str
    fans: int = 0


def _artist(d: dict) -> DeezerArtist:
    return DeezerArtist(
        id=int(d["id"]),
        name=d.get("name", ""),
        picture=d.get("picture_xl") or d.get("picture_big") or d.get("picture_medium") or d.get("picture", ""),
        fans=int(d.get("nb_fan", 0) or 0),
    )


async def search_artist(name: str) -> DeezerArtist | None:
    try:
        data = await client().get_json("search/artist", {"q": name, "limit": 10})
    except Exception as exc:
        log.warning("deezer search %r: %s", name, exc)
        return None
    exact = [d for d in data.get("data", []) if d.get("name", "").casefold() == name.casefold()]
    pool = exact or data.get("data", [])
    if not pool:
        return None
    return _artist(max(pool, key=lambda d: int(d.get("nb_fan", 0) or 0)))


async def get_artist(deezer_id: int) -> DeezerArtist | None:
    try:
        d = await client().get_json(f"artist/{deezer_id}")
    except Exception as exc:
        log.warning("deezer artist %s: %s", deezer_id, exc)
        return None
    if "error" in d:
        return None
    return _artist(d)


async def related_artists(deezer_id: int, limit: int = 20) -> list[DeezerArtist]:
    try:
        data = await client().get_json(f"artist/{deezer_id}/related", {"limit": limit})
    except Exception as exc:
        log.warning("deezer related %s: %s", deezer_id, exc)
        return []
    return [_artist(d) for d in data.get("data", [])]
