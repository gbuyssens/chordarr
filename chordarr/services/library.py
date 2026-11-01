"""Library scanning: match folders and files on disk to known releases.

Matching order, per release of a tracked artist:

1. an audio file whose tags carry the MusicBrainz release-group id (Picard, beets,
   Lidarr-tagged files all do) found anywhere under the artist folder;
2. a folder under the artist folder whose normalised name equals the normalised release
   title, with or without a trailing ``(year)``.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
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


@dataclass
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
    for dirpath, _dirs, files in root.walk():
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


async def scan_artist(session: AsyncSession, artist: Artist) -> dict:
    folder = artist_folder(artist)
    counters = {"matched": 0, "unmatched_folders": 0}
    if not folder or not folder.is_dir():
        return counters
    folders = scan_folder(folder)
    by_rg: dict[str, FolderInfo] = {}
    by_name: dict[str, FolderInfo] = {}
    for f in folders:
        for rg in f.rg_ids:
            by_rg[rg] = f
        by_name.setdefault(normalise(f.path.name), f)

    used: set[Path] = set()
    releases = (await session.scalars(select(Release).where(Release.artist_id == artist.id))).all()
    for rel in releases:
        hit = by_rg.get(rel.mbid) or by_name.get(normalise(rel.title))
        if hit is None:
            if rel.status == "have":
                rel.status, rel.path, rel.file_count = "missing", None, 0
            continue
        used.add(hit.path)
        was = rel.status
        rel.path = str(hit.path)
        rel.file_count = hit.audio_files
        if rel.track_count and hit.audio_files < rel.track_count:
            rel.status = "partial"
        else:
            rel.status = "have"
        counters["matched"] += 1
        if was != "have" and rel.status == "have":
            add_event(session, "release_imported", f"{artist.name}: {rel.title} is in the library", f"{hit.audio_files} files in {hit.path}", artist=artist, release=rel)
    counters["unmatched_folders"] = len([f for f in folders if f.path not in used])
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
