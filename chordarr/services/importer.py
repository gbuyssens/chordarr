"""Completed-download import: read tags, check they belong to the expected release, file
tracks under ``{root}/{artist}/{album} ({year})/{nn} - {title}.ext`` and mark the release
as present. The same code serves manual imports of folders that are already on disk.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chordarr.config import settings
from chordarr.models import Artist, Download, DownloadClient, Release, utcnow
from chordarr.services import downloadclients, mediaservers
from chordarr.services.events import add_event
from chordarr.services.library import AUDIO_EXT, artist_folder, normalise

log = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ImportError_(Exception):
    """Raised when a folder cannot be imported; the message is for the user."""


@dataclass
class TrackInfo:
    path: Path
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track: int | None = None
    disc: int | None = None
    rg_mbid: str | None = None


@dataclass
class ImportResult:
    destination: Path
    files: list[Path] = field(default_factory=list)
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


def _first(tags, *keys) -> str:
    for k in keys:
        try:
            v = tags.get(k)
        except Exception:
            v = None
        if v:
            v = v[0] if isinstance(v, (list, tuple)) else v
            return str(v).strip()
    return ""


def _num(value: str) -> int | None:
    m = re.match(r"\s*(\d+)", value or "")
    return int(m.group(1)) if m else None


def read_tags(path: Path) -> TrackInfo:
    info = TrackInfo(path=path)
    try:
        import mutagen

        audio = mutagen.File(path, easy=True)
    except Exception as exc:
        log.debug("tags of %s: %s", path, exc)
        audio = None
    tags = audio.tags if audio is not None and audio.tags else None
    if tags is not None:
        info.title = _first(tags, "title")
        info.artist = _first(tags, "artist")
        info.album = _first(tags, "album")
        info.album_artist = _first(tags, "albumartist", "album artist")
        info.track = _num(_first(tags, "tracknumber"))
        info.disc = _num(_first(tags, "discnumber"))
        info.rg_mbid = _first(tags, "musicbrainz_releasegroupid") or None
    if info.track is None:
        m = re.match(r"^\s*(?:\d+[\s.-]+)?(\d{1,3})\s*[-._ ]+\S", path.stem)
        info.track = int(m.group(1)) if m else None
    if not info.title:
        info.title = re.sub(r"^\s*\d{1,3}\s*[-._ ]+", "", path.stem).strip() or path.stem
    return info


def audio_files(src: Path) -> list[Path]:
    if src.is_file():
        return [src] if src.suffix.casefold() in AUDIO_EXT else []
    out = []
    for dirpath, dirs, files in src.walk():
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.casefold() not in ("sample", "samples")]
        out.extend(dirpath / f for f in files if Path(f).suffix.casefold() in AUDIO_EXT and not f.startswith("."))
    return sorted(out)


def sanitize(name: str) -> str:
    name = FORBIDDEN.sub("-", name).strip().rstrip(". ")
    return name[:180] or "_"


class _Safe(dict):
    def __missing__(self, key):
        return ""


def format_name(fmt: str, **values) -> str:
    return sanitize(fmt.format_map(_Safe(values)))


def release_folder(artist: Artist, release: Release) -> Path:
    base = artist_folder(artist)
    if base is None:
        raise ImportError_("no root folder configured (CHORDARR_ROOT_FOLDER)")
    return base / format_name(settings.album_folder_format, album=release.title, year=release.year or "", artist=artist.name)


def track_filename(track: TrackInfo, release: Release, artist: Artist, number: int, multi_disc: bool) -> str:
    fmt = settings.track_format
    if multi_disc and "{disc}" not in fmt:
        fmt = "{disc}-" + fmt
    name = format_name(fmt, nn=f"{number:02d}", track=number, disc=track.disc or 1, title=track.title, artist=track.artist or artist.name, album=release.title, year=release.year or "")
    return name + track.path.suffix.casefold()


def check_match(tracks: list[TrackInfo], release: Release, artist: Artist) -> str | None:
    """None when the tags fit the release, else a reason."""
    rg_ids = {t.rg_mbid for t in tracks if t.rg_mbid}
    if release.mbid in rg_ids:
        return None
    if rg_ids:
        return f"files are tagged with another release group ({', '.join(sorted(rg_ids))[:80]})"
    want_album = normalise(release.title)
    albums = {normalise(t.album) for t in tracks if t.album}
    if want_album and any(a == want_album or want_album in a or a in want_album for a in albums if a):
        return None
    artist_tokens = set(normalise(artist.name).split())
    credited = " ".join(t.artist + " " + t.album_artist for t in tracks)
    raw_album = next((t.album for t in tracks if t.album), "")
    if albums and artist_tokens and not artist_tokens <= set(normalise(credited).split()):
        return f"tags say album {raw_album!r} by {tracks[0].album_artist or tracks[0].artist!r}, not {release.title!r} by {artist.name!r}"
    if albums:
        return f"album tag {raw_album!r} does not match {release.title!r}"
    return None  # untagged files: trust the caller's association


def transfer(src: Path, dst: Path, mode: str) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        return "skipped"
    if mode == "move":
        shutil.move(str(src), str(dst))
        return "moved"
    if mode == "hardlink":
        try:
            if dst.exists():
                dst.unlink()
            os.link(src, dst)
            return "linked"
        except OSError:
            pass
    shutil.copy2(src, dst)
    return "copied"


def effective_mode(protocol: str | None) -> str:
    if settings.import_mode in ("hardlink", "copy", "move"):
        return settings.import_mode
    return "move" if protocol == "usenet" else "hardlink"


async def import_folder(
    session: AsyncSession,
    src: Path,
    release: Release,
    artist: Artist | None = None,
    mode: str | None = None,
    force: bool = False,
) -> ImportResult:
    artist = artist or await session.get(Artist, release.artist_id)
    if not src.exists():
        raise ImportError_(f"{src} does not exist (is a remote path mapping missing?)")
    files = audio_files(src)
    if not files:
        raise ImportError_(f"no audio file under {src}")
    tracks = [read_tags(p) for p in files]
    result = ImportResult(destination=release_folder(artist, release))
    reason = check_match(tracks, release, artist)
    if reason and not force:
        raise ImportError_(reason)
    if reason:
        result.warnings.append(reason)

    tracks.sort(key=lambda t: (t.disc or 1, t.track or 9999, t.path.name))
    discs = {t.disc or 1 for t in tracks}
    multi_disc = len(discs) > 1
    mode = mode or effective_mode(None)
    # Tag numbers are used when every track has one and they are unique per disc; otherwise
    # files are numbered in order.
    numbered = {(t.disc or 1, t.track) for t in tracks if t.track}
    use_tags = len(numbered) == len(tracks)
    seen: set[str] = set()
    for i, t in enumerate(tracks, start=1):
        number = t.track if use_tags else i
        name = track_filename(t, release, artist, number, multi_disc)
        if name in seen:
            name = f"{Path(name).stem} ({i}){Path(name).suffix}"
        seen.add(name)
        dst = result.destination / name
        outcome = transfer(t.path, dst, mode)
        if outcome == "skipped":
            result.skipped += 1
        result.files.append(dst)
    root = src if src.is_dir() else src.parent
    for img in sorted(root.iterdir()) if root.is_dir() else []:
        if img.is_file() and img.suffix.casefold() in IMAGE_EXT:
            transfer(img, result.destination / f"cover{img.suffix.casefold()}", "copy" if mode == "hardlink" else mode)
            break

    release.path = str(result.destination)
    release.file_count = len(result.files)
    release.status = "partial" if release.track_count and len(result.files) < release.track_count else "have"
    add_event(
        session,
        "release_imported",
        f"{artist.name}: {release.title} imported",
        f"{len(result.files)} files in {result.destination}" + (f" · {result.warnings[0]}" if result.warnings else ""),
        artist=artist,
        release=release,
        path=str(result.destination),
    )
    await session.flush()
    try:
        await mediaservers.notify_all(session, [result.destination])
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("media server notification failed: %s", exc)
    return result


async def guess_release(session: AsyncSession, src: Path) -> Release | None:
    """Which tracked release does this folder hold? By release-group tag, then by names."""
    files = audio_files(src)
    if not files:
        return None
    tracks = [read_tags(p) for p in files[:5]]
    for rg in {t.rg_mbid for t in tracks if t.rg_mbid}:
        rel = await session.scalar(select(Release).where(Release.mbid == rg).order_by(Release.kind.desc()))
        if rel:
            return rel
    album = next((t.album for t in tracks if t.album), "") or (src.name if src.is_dir() else "")
    credit = next((t.album_artist or t.artist for t in tracks if t.album_artist or t.artist), "")
    if not album:
        return None
    want = normalise(album)
    rows = (await session.execute(select(Release, Artist).join(Artist, Artist.id == Release.artist_id))).all()
    hits = [(r, a) for r, a in rows if normalise(r.title) == want]
    if credit:
        c = normalise(credit)
        exact = [(r, a) for r, a in hits if normalise(a.name) == c or set(normalise(a.name).split()) <= set(c.split())]
        hits = exact or hits
    hits.sort(key=lambda ra: (ra[0].kind != "own", ra[0].first_release_date or utcnow().date()))
    return hits[0][0] if hits else None


# --- queue ------------------------------------------------------------------------------


async def process_queue(session: AsyncSession) -> dict:
    """Poll the download clients, update the queue, import what finished."""
    counters = {"active": 0, "imported": 0, "failed": 0}
    active = (await session.scalars(select(Download).where(Download.status.in_(("grabbed", "downloading", "completed", "importing"))))).all()
    if not active:
        return counters
    clients = {c.id: c for c in (await session.scalars(select(DownloadClient))).all()}
    by_client: dict[int | None, list[Download]] = {}
    for d in active:
        by_client.setdefault(d.download_client_id, []).append(d)
    for client_id, downloads in by_client.items():
        cfg = clients.get(client_id)
        if cfg is None:
            for d in downloads:
                _fail(session, d, "download client was removed")
                counters["failed"] += 1
            continue
        try:
            async with downloadclients.make_client(cfg) as dc:
                items = {i.client_id.lower(): i for i in await dc.items()}
            cfg.last_error = None
        except Exception as exc:
            cfg.last_error = f"{utcnow():%Y-%m-%d %H:%M} {exc}"[:500]
            log.warning("download client %s unreachable: %s", cfg.name, exc)
            continue
        for d in downloads:
            item = items.get((d.client_id or "").lower())
            if item is None and not d.client_id:
                item = next((i for i in items.values() if i.name == d.title), None)
                if item:
                    d.client_id = item.client_id
            if item is None:
                if d.status == "grabbed" and utcnow() - d.added_at < timedelta(minutes=15):
                    continue  # the client may still be fetching metadata
                _fail(session, d, "no longer in the download client")
                counters["failed"] += 1
                continue
            d.progress = item.progress
            if item.status == "failed":
                _fail(session, d, f"download client reports failure ({item.message})")
                counters["failed"] += 1
            elif item.status == "completed":
                await _import_download(session, d, cfg, item, counters)
            else:
                d.status = "downloading"
                d.message = item.message
                counters["active"] += 1
    await session.flush()
    return counters


def _fail(session: AsyncSession, d: Download, message: str) -> None:
    d.status = "failed"
    d.message = message
    add_event(session, "download_failed", f"Download failed: {d.title}", message, release=d.release)


async def _import_download(session: AsyncSession, d: Download, cfg: DownloadClient, item: downloadclients.ClientItem, counters: dict) -> None:
    if d.release is None:
        d.status, d.message = "warning", "completed, but the release it was grabbed for no longer exists"
        return
    d.status = "importing"
    local = downloadclients.map_path(cfg, item.path or "")
    d.output_path = str(local)
    try:
        result = await import_folder(session, local, d.release, mode=effective_mode(d.protocol))
    except ImportError_ as exc:
        d.status, d.message = "warning", f"import needs attention: {exc}"
        add_event(session, "import_failed", f"Import of {d.title} needs attention", str(exc), release=d.release)
        return
    except Exception as exc:
        log.exception("import of %s failed", d.title)
        d.status, d.message = "warning", f"import failed: {exc}"[:500]
        add_event(session, "import_failed", f"Import of {d.title} failed", str(exc)[:400], release=d.release)
        return
    d.status, d.progress, d.imported_at = "imported", 1.0, utcnow()
    d.message = f"{len(result.files)} files → {result.destination}" + (f" · {result.warnings[0]}" if result.warnings else "")
    counters["imported"] += 1
    if cfg.remove_completed or d.protocol == "usenet":
        try:
            async with downloadclients.make_client(cfg) as dc:
                await dc.remove(d.client_id or "", delete_files=d.protocol == "usenet")
        except Exception as exc:
            log.warning("could not remove %s from %s: %s", d.title, cfg.name, exc)
