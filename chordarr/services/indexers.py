"""Torznab / Newznab client: capabilities, release search, RSS.

Both protocols share one XML dialect (``rss/channel/item`` plus ``torznab:attr`` or
``newznab:attr`` elements). Prowlarr exposes every indexer it manages that way, so this
module is all Chordarr needs to talk to Prowlarr as well as to a bare indexer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import httpx

from chordarr.config import settings
from chordarr.models import Indexer

log = logging.getLogger(__name__)

MUSIC_CATEGORIES = (3000, 3010, 3020, 3030, 3040, 3050, 3060)
LOSSLESS_RE = re.compile(r"\b(flac|alac|ape|wav|aiff|dsd|dsf|lossless|24\s?bits?|16\s?bits?)\b", re.I)
HIGH_RE = re.compile(r"(?<!\d)320(?!\d)|\bv0\b|(?:aac|m4a|opus)[\s.-]?256(?![\d])", re.I)
LOW_RE = re.compile(r"\b(mp3|aac|m4a|ogg|v2)\b|(?<!\d)(128|160|192|256)(?!\d)", re.I)


@dataclass
class Candidate:
    title: str
    guid: str
    download_url: str
    indexer_id: int
    indexer_name: str
    protocol: str  # torrent | usenet
    size: int | None = None
    seeders: int | None = None
    leechers: int | None = None
    published: datetime | None = None
    categories: list[int] = field(default_factory=list)
    info_url: str | None = None
    quality: str = "unknown"  # lossless | high | low | unknown
    score: int = 0
    rejections: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return not self.rejections

    @property
    def size_label(self) -> str:
        return human_size(self.size)


def human_size(size: int | None) -> str:
    if not size:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "?"


def quality_of(title: str, categories: list[int] | None = None) -> str:
    if LOSSLESS_RE.search(title) or 3040 in (categories or []):
        return "lossless"
    if HIGH_RE.search(title):
        return "high"
    if LOW_RE.search(title):
        return "low"
    return "unknown"


def _text(item: ET.Element, name: str) -> str | None:
    el = item.find(name)
    return el.text.strip() if el is not None and el.text else None


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def parse_feed(xml_text: str, indexer: Indexer) -> list[Candidate]:
    """Items of a Torznab/Newznab answer. Raises ValueError on an indexer error document."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"not XML: {str(exc)[:80]}") from exc
    if root.tag == "error":
        raise ValueError(f"indexer error {root.get('code')}: {root.get('description')}")
    out: list[Candidate] = []
    for item in root.iter("item"):
        attrs: dict[str, list[str]] = {}
        for el in item:
            if el.tag.endswith("attr") and el.get("name"):
                attrs.setdefault(el.get("name"), []).append(el.get("value", ""))
        enclosure = item.find("enclosure")
        url = (enclosure.get("url") if enclosure is not None else None) or _text(item, "link") or ""
        size = _int(_text(item, "size")) or _int(attrs.get("size", [None])[0]) or _int(enclosure.get("length") if enclosure is not None else None)
        cats = [c for c in (_int(v) for v in attrs.get("category", [])) if c is not None]
        cats += [c for c in (_int(el.text) for el in item.findall("category")) if c is not None and c not in cats]
        published = None
        if pub := _text(item, "pubDate"):
            try:
                published = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                published = None
        title = _text(item, "title") or ""
        out.append(
            Candidate(
                title=title,
                guid=_text(item, "guid") or url,
                download_url=url,
                indexer_id=indexer.id,
                indexer_name=indexer.name,
                protocol=indexer.protocol,
                size=size,
                seeders=_int(attrs.get("seeders", [None])[0]),
                leechers=_int(attrs.get("leechers", attrs.get("peers", [None]))[0]),
                published=published,
                categories=cats,
                info_url=_text(item, "comments") or attrs.get("infourl", [None])[0],
                quality=quality_of(title, cats),
            )
        )
    return out


def parse_caps(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    if root.tag == "error":
        raise ValueError(f"indexer error {root.get('code')}: {root.get('description')}")
    caps: dict = {"search_params": [], "music_params": [], "categories": {}}
    searching = root.find("searching")
    if searching is not None:
        for el in searching:
            params = [p.strip() for p in (el.get("supportedParams") or "").split(",") if p.strip()]
            if el.tag == "search" and el.get("available") == "yes":
                caps["search_params"] = params
            elif el.tag in ("music-search", "audio-search") and el.get("available") == "yes":
                caps["music_params"] = sorted(set(caps["music_params"]) | set(params))
    for cat in root.iter("category"):
        if cat.get("id"):
            caps["categories"][cat.get("id")] = cat.get("name", "")
        for sub in cat.findall("subcat"):
            if sub.get("id"):
                caps["categories"][sub.get("id")] = sub.get("name", "")
    return caps


async def fetch(indexer: Indexer, params: dict, timeout: float = 40) -> str:
    query = {"apikey": indexer.api_key, **params}
    if indexer.additional_parameters:
        for part in indexer.additional_parameters.lstrip("&?").split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                query.setdefault(k, v)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": settings.user_agent}, follow_redirects=True) as client:
        r = await client.get(indexer.api_url, params=query)
        r.raise_for_status()
        return r.text


async def caps(indexer: Indexer) -> dict:
    return parse_caps(await fetch(indexer, {"t": "caps"}, timeout=20))


def _cat_param(indexer: Indexer) -> str:
    cats = [int(c) for c in (indexer.categories or []) if int(c) in MUSIC_CATEGORIES or int(c) >= 100000]
    return ",".join(str(c) for c in cats or [3000])


async def search(indexer: Indexer, artist: str, album: str, mbid: str | None = None, limit: int = 100) -> list[Candidate]:
    """Music search: ``artist``/``album`` parameters when the indexer takes them, else a text query."""
    music_params = set((indexer.capabilities or {}).get("music_params") or [])
    params: dict = {"t": "music", "cat": _cat_param(indexer), "limit": limit, "extended": 1}
    if artist and "artist" in music_params and "album" in music_params:
        params["artist"], params["album"] = artist, album
    else:
        params["q"] = f"{artist} {album}".strip()
    if mbid and "mbid" in music_params:
        params["mbid"] = mbid
    text = await fetch(indexer, params)
    items = parse_feed(text, indexer)
    if not items and "q" not in params:
        # Some indexers advertise artist/album but only honour q.
        items = parse_feed(await fetch(indexer, {"t": "search", "cat": params["cat"], "q": f"{artist} {album}".strip(), "limit": limit}), indexer)
    return items


async def rss(indexer: Indexer, limit: int = 100) -> list[Candidate]:
    text = await fetch(indexer, {"t": "search", "cat": _cat_param(indexer), "limit": limit, "extended": 1})
    return parse_feed(text, indexer)


async def test(indexer: Indexer) -> dict:
    """Fetch capabilities; raises on failure, returns the parsed caps on success."""
    return await caps(indexer)
