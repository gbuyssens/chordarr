"""Library scanning: match folders and files on disk to known releases.

Matching order, per release of a tracked artist:

1. an audio file whose tags carry the MusicBrainz release-group id (Picard, beets,
   Lidarr-tagged files all do) found anywhere under the artist folder;
2. a folder under the artist folder whose normalised name equals the normalised release
   title, with or without a trailing ``(year)``.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chordarr.config import settings
from chordarr.models import Artist, Release
from chordarr.services.events import add_event

log = logging.getLogger(__name__)

AUDIO_EXT = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wv", ".ape", ".aiff", ".alac", ".dsf"}
RG_TAG_KEYS = ("musicbrainz_releasegroupid", "MusicBrainz Release Group Id", "----:com.apple.iTunes:MusicBrainz Release Group Id", "TXXX:MusicBrainz Release Group Id")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    text = re.sub(r"\s*[\(\[]\s*(19|20)\d{2}\s*[\)\]]\s*$", "", text)  # trailing (year)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


@dataclass(eq=False)
class FolderInfo:
    path: Path
    audio_files: int = 0
    rg_ids: set[str] = field(default_factory=set)


def _read_rg_id(path: Path) -> str | None:
    try:
        import mutagen

        audio = mutagen.File(path, easy=False)
    except Exception:
        return None
    if audio is None or not audio.tags:
        return None
    tags = audio.tags
    for key in RG_TAG_KEYS:
        try:
            value = tags.get(key) if hasattr(tags, "get") else None
        except Exception:
            value = None
        if value:
            v = value[0] if isinstance(value, (list, tuple)) else value
            v = getattr(v, "text", v)
            v = v[0] if isinstance(v, (list, tuple)) else v
            return str(v).strip() or None
    # ID3 frames are keyed "TXXX:MusicBrainz Release Group Id"; iterate as a fallback.
    try:
        for key in tags:
            if "release group id" in str(key).casefold().replace("_", " "):
                v = tags[key]
                v = getattr(v, "text", v)
                v = v[0] if isinstance(v, (list, tuple)) else v
                return str(v).strip() or None
    except Exception:
        pass
    return None


def scan_folder(root: Path, read_tags: bool = True, max_tag_reads_per_folder: int = 3) -> list[FolderInfo]:
    """Return every folder under ``root`` that directly contains audio files."""
    out: list[FolderInfo] = []
    if not root.is_dir():
        return out
    for dirpath_s, _dirs, files in os.walk(root):  # os.walk: Path.walk needs Python 3.12
        dirpath = Path(dirpath_s)
        audio = [f for f in files if Path(f).suffix.casefold() in AUDIO_EXT]
        if not audio:
            continue
        info = FolderInfo(path=dirpath, audio_files=len(audio))
        if read_tags:
            for name in sorted(audio)[:max_tag_reads_per_folder]:
                rg = _read_rg_id(dirpath / name)
                if rg:
                    info.rg_ids.add(rg)
        out.append(info)
    return out


def artist_folder(artist: Artist) -> Path | None:
    if artist.path:
        return Path(artist.path)
    if settings.root_folder:
        return Path(settings.root_folder) / settings.artist_folder_format.format(artist=artist.name.replace("/", "-"))
    return None


def folder_year(name: str) -> int | None:
    m = re.search(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]\s*$", name)
    return int(m.group(1)) if m else None


def match_folders(folders: list[FolderInfo], releases: list[Release]) -> dict[int, FolderInfo]:
    """Pair each folder with at most one release and each release with at most one folder.

    Release-group tags win. Then folder names: when several releases share a title (an
    album and its deluxe edition, a single named like the album), the one whose year matches
    the folder's ``(year)`` suffix wins, then the artist's own releases over appearances.
    """
    by_id = {r.mbid: r for r in releases}
    by_title: dict[str, list[Release]] = {}
    for r in releases:
        by_title.setdefault(normalise(r.title), []).append(r)
    paired: dict[int, FolderInfo] = {}
    taken: set[int] = set()

    def take(rel: Release, folder: FolderInfo) -> None:
        paired[rel.id] = folder
        taken.add(rel.id)

    for f in folders:
        for rg in f.rg_ids:
            rel = by_id.get(rg)
            if rel and rel.id not in taken:
                take(rel, f)
                break
    for f in folders:
        if f in paired.values():
            continue
        candidates = [r for r in by_title.get(normalise(f.path.name), []) if r.id not in taken]
        if not candidates:
            continue
        year = folder_year(f.path.name)
        candidates.sort(key=lambda r: (r.year != year if year else False, r.kind != "own", r.first_release_date or date.max))
        take(candidates[0], f)
    return paired


async def scan_artist(session: AsyncSession, artist: Artist) -> dict:
    folder = artist_folder(artist)
    counters = {"matched": 0, "unmatched_folders": 0}
    if not folder or not folder.is_dir():
        return counters
    folders = scan_folder(folder)
    releases = (await session.scalars(select(Release).where(Release.artist_id == artist.id))).all()
    paired = match_folders(folders, releases)

    for rel in releases:
        hit = paired.get(rel.id)
        if hit is None:
            if rel.status in ("have", "partial"):
                rel.status, rel.path, rel.file_count = "missing", None, 0
            continue
        was = rel.status
        rel.path = str(hit.path)
        rel.file_count = hit.audio_files
        rel.status = "partial" if rel.track_count and hit.audio_files < rel.track_count else "have"
        counters["matched"] += 1
        if was != "have" and rel.status == "have":
            add_event(session, "release_imported", f"{artist.name}: {rel.title} is in the library", f"{hit.audio_files} files in {hit.path}", artist=artist, release=rel)
    used = set(paired.values())
    counters["unmatched_folders"] = len([f for f in folders if f not in used])
    await session.flush()
    return counters


async def scan_all(session: AsyncSession) -> dict:
    totals = {"artists": 0, "matched": 0, "unmatched_folders": 0}
    for artist in (await session.scalars(select(Artist))).all():
        c = await scan_artist(session, artist)
        totals["artists"] += 1
        totals["matched"] += c["matched"]
        totals["unmatched_folders"] += c["unmatched_folders"]
    add_event(session, "library_scanned", "Library scanned", f"{totals['matched']} releases matched across {totals['artists']} artists", **totals)
    return totals


def unknown_artist_folders(known_paths: set[str]) -> list[Path]:
    """Top-level folders in the root folder that belong to no tracked artist."""
    root = settings.root_folder
    if not root or not Path(root).is_dir():
        return []
    return sorted(p for p in Path(root).iterdir() if p.is_dir() and str(p) not in known_paths and not p.name.startswith("."))
