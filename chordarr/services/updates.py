"""Update check against GitHub releases. No self-update: packages and images do that."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

import httpx

from chordarr import __version__
from chordarr.config import settings

log = logging.getLogger(__name__)
REPO = "gbuyssens/chordarr"
_cache: dict = {"at": None, "releases": []}


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


async def check(force: bool = False) -> list[dict]:
    if not force and _cache["at"] and datetime.now(UTC).replace(tzinfo=None) - _cache["at"] < timedelta(hours=6):
        return _cache["releases"]
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": settings.user_agent, "Accept": "application/vnd.github+json"}) as c:
            r = await c.get(f"https://api.github.com/repos/{REPO}/releases", params={"per_page": 10})
            r.raise_for_status()
            rels = [x for x in r.json() if not x.get("draft")]
    except Exception as exc:
        log.warning("update check failed: %s", exc)
        rels = _cache["releases"]
    _cache.update(at=datetime.now(UTC).replace(tzinfo=None), releases=rels)
    return rels


def latest_version(releases: list[dict]) -> str | None:
    versions = [r["tag_name"].lstrip("v") for r in releases if not r.get("prerelease")]
    return max(versions, key=_vtuple) if versions else None


def is_newer(candidate: str | None, current: str = __version__) -> bool:
    return bool(candidate) and _vtuple(candidate) > _vtuple(current)


async def lidarr_shape(current: str = __version__) -> list[dict]:
    rels = await check()
    latest = latest_version(rels)
    out = []
    for r in rels:
        v = r["tag_name"].lstrip("v")
        body = r.get("body") or ""
        out.append({
            "version": v,
            "branch": "main",
            "releaseDate": r.get("published_at"),
            "fileName": f"chordarr-{v}",
            "url": r.get("html_url"),
            "installed": v == current,
            "installedOn": None,
            "installable": False,
            "latest": v == latest,
            "changes": {"new": [line.lstrip("-* ").strip() for line in body.splitlines() if line.strip().startswith(("-", "*"))], "fixed": []},
            "hash": "",
        })
    return out
