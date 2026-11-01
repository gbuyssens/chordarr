"""Media servers: ask Jellyfin (or Emby) and Plex to look at one folder after an import.

Only targeted scans are issued, never a full library refresh: Jellyfin gets
``POST /Library/Media/Updated`` with the folder path, Plex gets
``GET /library/sections/{id}/refresh?path=`` on the music section that contains it.
"""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chordarr import __version__
from chordarr.config import settings
from chordarr.models import MediaServer, utcnow

log = logging.getLogger(__name__)

MEDIA_SERVER_TYPES = {
    "jellyfin": {"label": "Jellyfin / Emby", "fields": ["url", "api_key", "local_path", "remote_path"]},
    "plex": {"label": "Plex", "fields": ["url", "api_key", "section", "local_path", "remote_path"]},
}


def remote_path(server: MediaServer, local: Path | str) -> str:
    """Path of ``local`` as the media server sees it."""
    local_s = str(local)
    if server.local_path and server.remote_path:
        prefix = server.local_path.rstrip("/")
        if local_s == prefix or local_s.startswith(prefix + "/"):
            rel = local_s[len(prefix) :].lstrip("/")
            base = server.remote_path.rstrip("/")
            return f"{base}/{PurePosixPath(rel)}" if rel else base
    return local_s


def _jellyfin_headers(server: MediaServer) -> dict:
    auth = f'MediaBrowser Token="{server.api_key}", Client="Chordarr", Device="Chordarr", DeviceId="chordarr", Version="{__version__}"'
    return {"Authorization": auth, "X-Emby-Token": server.api_key, "Accept": "application/json", "User-Agent": settings.user_agent}


def _plex_headers(server: MediaServer) -> dict:
    return {
        "X-Plex-Token": server.api_key,
        "X-Plex-Client-Identifier": "chordarr",
        "X-Plex-Product": "Chordarr",
        "X-Plex-Version": __version__,
        "Accept": "application/json",
        "User-Agent": settings.user_agent,
    }


async def test(server: MediaServer) -> str:
    base = server.url.rstrip("/")
    async with httpx.AsyncClient(timeout=20) as client:
        if server.type == "jellyfin":
            r = await client.get(f"{base}/System/Info", headers=_jellyfin_headers(server))
            r.raise_for_status()
            info = r.json()
            return f"{info.get('ProductName') or 'Jellyfin'} {info.get('Version', '')} ({info.get('ServerName', '')})".strip()
        if server.type == "plex":
            r = await client.get(f"{base}/identity", headers=_plex_headers(server))
            r.raise_for_status()
            mc = r.json().get("MediaContainer", {})
            sections = await _plex_sections(client, server)
            music = [s["title"] for s in sections if s.get("type") == "artist"]
            return f"Plex {mc.get('version', '')} · music sections: {', '.join(music) or 'none'}"
    raise ValueError(f"unknown media server type {server.type}")


async def _plex_sections(client: httpx.AsyncClient, server: MediaServer) -> list[dict]:
    r = await client.get(f"{server.url.rstrip('/')}/library/sections", headers=_plex_headers(server))
    r.raise_for_status()
    return r.json().get("MediaContainer", {}).get("Directory", []) or []


async def notify(server: MediaServer, paths: list[Path | str]) -> None:
    base = server.url.rstrip("/")
    remote = [remote_path(server, p) for p in paths]
    async with httpx.AsyncClient(timeout=30) as client:
        if server.type == "jellyfin":
            body = {"Updates": [{"Path": p, "UpdateType": "Created"} for p in remote]}
            r = await client.post(f"{base}/Library/Media/Updated", json=body, headers=_jellyfin_headers(server))
            r.raise_for_status()
            return
        if server.type == "plex":
            sections = await _plex_sections(client, server)
            for p in remote:
                key = server.section or _plex_section_for(sections, p)
                if not key:
                    raise ValueError(f"no Plex music section contains {p}")
                r = await client.get(f"{base}/library/sections/{key}/refresh", params={"path": p}, headers=_plex_headers(server))
                r.raise_for_status()
            return
    raise ValueError(f"unknown media server type {server.type}")


def _plex_section_for(sections: list[dict], path: str) -> str | None:
    best, best_len = None, -1
    for s in sections:
        if s.get("type") != "artist":
            continue
        for loc in s.get("Location", []) or []:
            root = (loc.get("path") or "").rstrip("/")
            if root and (path == root or path.startswith(root + "/")) and len(root) > best_len:
                best, best_len = str(s.get("key")), len(root)
    return best


async def notify_all(session: AsyncSession, paths: list[Path | str]) -> None:
    """Best effort: a media server that fails is recorded, never blocks an import."""
    servers = (await session.scalars(select(MediaServer).where(MediaServer.enabled.is_(True)))).all()
    for server in servers:
        try:
            await notify(server, paths)
            server.last_error = None
        except Exception as exc:
            server.last_error = f"{utcnow():%Y-%m-%d %H:%M} {exc}"[:500]
            log.warning("media server %s: %s", server.name, exc)
