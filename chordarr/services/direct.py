"""Direct sources through streamrip: Qobuz, Deezer and Tidal with the user's own accounts.

streamrip (https://github.com/nathom/streamrip) is used as a library, in-process, on the
same event loop: search an album on each enabled source, then resolve and rip it into the
client's download folder. The result goes through the normal completed-download import,
like a torrent or an NZB would. streamrip is optional: ``pip install chordarr[streamrip]``.

Credentials live in a streamrip ``config.toml`` that Chordarr writes under its data
directory, one per download client of type ``streamrip``. Qobuz takes an email and the
md5 of the password (what streamrip expects), Deezer an ARL cookie, Tidal the tokens that
``rip config --tidal`` produces (device login), pasted in the advanced fields.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from chordarr.config import settings
from chordarr.models import DownloadClient
from chordarr.services.indexers import Candidate

log = logging.getLogger(__name__)

SOURCES = {
    "qobuz": {"label": "Qobuz", "max_quality": 4},
    "deezer": {"label": "Deezer", "max_quality": 2},
    "tidal": {"label": "Tidal", "max_quality": 3},
}
# streamrip quality levels: 0 128k, 1 320k, 2 CD 16/44.1, 3 24/<=96, 4 24/<=192
QUALITY_LABELS = {0: "128 kbps", 1: "320 kbps", 2: "CD 16 bit / 44.1 kHz", 3: "Hi-Res 24 bit / 96 kHz", 4: "Hi-Res 24 bit / 192 kHz"}
FIELDS = ["download_folder", "quality", "sources", "qobuz_email", "qobuz_password", "deezer_arl", "tidal_user_id", "tidal_country_code", "tidal_access_token", "tidal_refresh_token", "tidal_token_expiry"]


def available() -> bool:
    try:
        import streamrip  # noqa: F401
    except ImportError:
        return False
    return True


# --- jobs (the "queue" of a direct source lives in memory, mirrored on Download rows) ------


@dataclass
class Job:
    id: str
    title: str
    source: str
    album_id: str
    status: str = "queued"  # queued | downloading | completed | failed
    progress: float = 0.0
    path: str | None = None
    message: str = ""
    started: datetime = field(default_factory=datetime.utcnow)


JOBS: dict[str, Job] = {}
_slots: asyncio.Semaphore | None = None


def slots() -> asyncio.Semaphore:
    """At most ``settings.direct_concurrency`` albums rip at the same time; the rest wait as queued."""
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(max(1, int(getattr(settings, "direct_concurrency", 2))))
    return _slots


# --- configuration --------------------------------------------------------------------------


def client_dir(client: DownloadClient) -> Path:
    return settings.data_dir / "streamrip" / str(client.id or "new")


def download_folder(client: DownloadClient) -> Path:
    folder = (client.settings or {}).get("download_folder") or ""
    return Path(folder) if folder else client_dir(client) / "downloads"


def enabled_sources(client: DownloadClient) -> list[str]:
    raw = (client.settings or {}).get("sources") or "qobuz,deezer,tidal"
    names = [str(s) for s in raw] if isinstance(raw, list) else [s.strip() for s in str(raw).split(",")]
    return [s for s in names if s in SOURCES and _has_credentials(client, s)]


def _has_credentials(client: DownloadClient, source: str) -> bool:
    s = client.settings or {}
    if source == "qobuz":
        return bool(s.get("qobuz_email") and s.get("qobuz_password"))
    if source == "deezer":
        return bool(s.get("deezer_arl"))
    if source == "tidal":
        return bool(s.get("tidal_access_token") and s.get("tidal_refresh_token"))
    return False


def clean_error(exc: BaseException) -> str:
    """streamrip repeats the request parameters (password hash included) in its errors."""
    text = str(exc) or exc.__class__.__name__
    text = text.split(" from params", 1)[0]
    return text[:200] or exc.__class__.__name__


def md5_password(value: str) -> str:
    """Qobuz wants the md5 of the password; accept an already-hashed value unchanged."""
    if re.fullmatch(r"[0-9a-f]{32}", value or ""):
        return value
    return hashlib.md5(value.encode()).hexdigest()  # noqa: S324 - Qobuz's own scheme


def write_config(client: DownloadClient):
    """Write streamrip's config.toml for this client and return the loaded Config."""
    from streamrip.config import BLANK_CONFIG_PATH, Config

    folder = client_dir(client)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "config.toml"
    if not path.is_file():
        shutil.copy(BLANK_CONFIG_PATH, path)
    config = Config(str(path))
    s = client.settings or {}
    quality = int(s.get("quality") or 3)
    f = config.file
    f.downloads.folder = str(download_folder(client))
    f.downloads.source_subdirectories = False
    f.database.downloads_enabled = False
    f.database.failed_downloads_enabled = False
    f.qobuz.quality = min(quality, 4)
    f.qobuz.email_or_userid = str(s.get("qobuz_email") or "")
    f.qobuz.password_or_token = str(s.get("qobuz_password") or "")
    f.qobuz.use_auth_token = False
    f.deezer.quality = min(quality, 2)
    f.deezer.arl = str(s.get("deezer_arl") or "")
    f.deezer.use_deezloader = False
    f.tidal.quality = min(quality, 3)
    for key in ("user_id", "country_code", "access_token", "refresh_token", "token_expiry"):
        if s.get(f"tidal_{key}"):
            setattr(f.tidal, key, str(s[f"tidal_{key}"]))
    f.cli.progress_bars = False
    f.cli.text_output = False
    f.set_modified()
    config.save_file()
    config = Config(str(path))  # re-read so session == file
    config.session.cli.progress_bars = False
    config.session.cli.text_output = False
    download_folder(client).mkdir(parents=True, exist_ok=True)
    return config


_clients: dict[tuple[int, str], object] = {}
_configs: dict[int, object] = {}


def forget(client_id: int) -> None:
    for key in [k for k in _clients if k[0] == client_id]:
        _clients.pop(key, None)
    _configs.pop(client_id, None)


async def source_client(client: DownloadClient, source: str):
    """A logged-in streamrip client for one source, cached per download client."""
    from streamrip.client import DeezerClient, QobuzClient, TidalClient

    key = (client.id, source)
    cached = _clients.get(key)
    if cached is not None and getattr(cached, "logged_in", False):
        return cached
    config = _configs.get(client.id)
    if config is None:
        config = _configs[client.id] = write_config(client)
    cls = {"qobuz": QobuzClient, "deezer": DeezerClient, "tidal": TidalClient}[source]
    sc = cls(config)
    await sc.login()
    _clients[key] = sc
    return sc


# --- search ---------------------------------------------------------------------------------


def _quality_for(source: str, item: dict, configured: int) -> tuple[str, str]:
    """(Chordarr quality bucket, human detail) for one search result."""
    if source == "qobuz":
        depth = int(item.get("maximum_bit_depth") or 16)
        rate = float(item.get("maximum_sampling_rate") or 44.1)
        if configured <= 1:
            return "high", "320 kbps"
        if configured >= 3 and depth > 16:
            return "lossless", f"{depth} bit / {rate:g} kHz"
        return "lossless", "16 bit / 44.1 kHz"
    if source == "deezer":
        return ("lossless", "FLAC") if configured >= 2 else ("high", "320 kbps")
    if source == "tidal":
        return ("lossless", "FLAC") if configured >= 2 else ("high", "320 kbps AAC")
    return "unknown", ""


def candidates_from_pages(client: DownloadClient, source: str, pages: list[dict]) -> list[Candidate]:
    """Turn streamrip search pages into Chordarr candidates (pure, testable)."""
    from streamrip.metadata import SearchResults

    quality = int((client.settings or {}).get("quality") or 3)
    raw_items: list[dict] = []
    for page in pages:
        if source == "qobuz":
            raw_items.extend(page.get("albums", {}).get("items", []))
        elif source == "deezer":
            raw_items.extend(page.get("data", []))
        elif source == "tidal":
            raw_items.extend(page.get("items", []))
    summaries = SearchResults.from_pages(source, "album", pages).results
    out = []
    for item, summary in zip(raw_items, summaries, strict=False):
        bucket, detail = _quality_for(source, item, quality)
        year = (summary.date_released or "")[:4]
        title = f"{summary.artist} - {summary.name}" + (f" ({year})" if year.isdigit() else "") + f" [{SOURCES[source]['label']} {detail}]"
        out.append(
            Candidate(
                title=title,
                guid=f"direct:{client.id}:{source}:{summary.id}",
                download_url=f"direct:{client.id}:{source}:{summary.id}",
                indexer_id=0,
                indexer_name=f"{SOURCES[source]['label']} ({client.name})",
                protocol="direct",
                size=None,
                seeders=None,
                categories=[3040 if bucket == "lossless" else 3010],
                info_url=_info_url(source, summary.id),
                quality=bucket,
            )
        )
    return out


def _info_url(source: str, album_id: str) -> str:
    return {
        "qobuz": f"https://open.qobuz.com/album/{album_id}",
        "deezer": f"https://www.deezer.com/album/{album_id}",
        "tidal": f"https://tidal.com/browse/album/{album_id}",
    }.get(source, "")


async def search(client: DownloadClient, artist: str, title: str, limit: int = 15) -> list[Candidate]:
    if not available():
        return []
    out: list[Candidate] = []
    for source in enabled_sources(client):
        try:
            sc = await source_client(client, source)
            pages = await sc.search("album", f"{artist} {title}".strip(), limit=limit)
            out.extend(candidates_from_pages(client, source, pages))
        except Exception as exc:
            log.warning("streamrip %s search failed: %s", source, clean_error(exc))
            _clients.pop((client.id, source), None)
            raise RuntimeError(f"{SOURCES[source]['label']}: {clean_error(exc)}") from exc
    return out


def parse_ref(ref: str) -> tuple[int, str, str]:
    """``direct:<client id>:<source>:<album id>``."""
    parts = ref.split(":", 3)
    if len(parts) != 4 or parts[0] != "direct":
        raise ValueError(f"not a direct reference: {ref}")
    return int(parts[1]), parts[2], parts[3]


# --- download ------------------------------------------------------------------------------


async def _run(job: Job, client: DownloadClient) -> None:
    from streamrip import db as rip_db
    from streamrip.media import PendingAlbum

    try:
        async with slots():
            if job.id not in JOBS:
                return  # removed while waiting
            log.info("streamrip job %s: %s on %s (album %s)", job.id, job.title, job.source, job.album_id)
            sc = await source_client(client, job.source)
            config = _configs[client.id]
            job.status = "downloading"
            pending = PendingAlbum(job.album_id, sc, config, rip_db.Database(rip_db.Dummy(), rip_db.Dummy()))
            album = await pending.resolve()
            if album is None:
                raise RuntimeError("album not available on this source")
            job.path = album.folder
            job.progress = 0.1
            await album.rip()
            job.progress = 1.0
            job.status = "completed"
            job.message = f"{len(album.tracks)} tracks"
            log.info("streamrip job %s done: %s → %s", job.id, job.title, job.path)
    except Exception as exc:
        log.warning("streamrip download failed: %s: %s", job.title, clean_error(exc))
        job.status = "failed"
        job.message = clean_error(exc)


def start(client: DownloadClient, source: str, album_id: str, title: str) -> Job:
    job = Job(id=f"rip-{uuid.uuid4().hex[:12]}", title=title, source=source, album_id=album_id)
    JOBS[job.id] = job
    asyncio.create_task(_run(job, client))
    return job


async def resume(session) -> int:
    """Restart direct downloads that were in flight when the process stopped.

    Jobs live in memory, so a restart (deploy, reboot) loses them; the Download rows
    remember what was asked. Each one is started again on the same source and album;
    streamrip overwrites what it had already written.
    """
    from sqlalchemy import select

    from chordarr.models import Download

    rows = (await session.scalars(select(Download).where(Download.protocol == "direct", Download.status.in_(("grabbed", "downloading"))))).all()
    restarted = 0
    for d in rows:
        if d.client_id and d.client_id in JOBS:
            continue
        try:
            client_id, source, album_id = parse_ref(d.download_url)
        except ValueError:
            continue
        client = await session.get(DownloadClient, client_id)
        if client is None or not client.enabled:
            d.status, d.message = "failed", "direct source removed while downloading"
            continue
        job = start(client, source, album_id, d.title)
        d.client_id = job.id
        d.status, d.progress, d.message = "grabbed", 0.0, "resumed after restart"
        restarted += 1
    await session.flush()
    return restarted


class StreamripClient:
    """The download-client interface (test, add, items, remove) over the job registry."""

    def __init__(self, client: DownloadClient):
        self.cfg = client

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def test(self) -> str:
        if not available():
            raise RuntimeError("streamrip is not installed: pip install 'chordarr[streamrip]'")
        sources = enabled_sources(self.cfg)
        if not sources:
            raise RuntimeError("no source has credentials (Qobuz email + password, Deezer ARL, or Tidal tokens)")
        forget(self.cfg.id)
        ok = []
        for source in sources:
            try:
                await source_client(self.cfg, source)
            except Exception as exc:
                raise RuntimeError(f"{SOURCES[source]['label']}: {clean_error(exc)}") from exc
            ok.append(SOURCES[source]["label"])
        return "streamrip: " + ", ".join(ok) + f" logged in · {download_folder(self.cfg)}"

    async def add(self, name: str, torrent: bytes | None = None, url: str | None = None, **_) -> str:
        if not url:
            raise RuntimeError("nothing to add")
        _client_id, source, album_id = parse_ref(url)
        job = start(self.cfg, source, album_id, name)
        return job.id

    async def items(self):
        from chordarr.services.downloadclients import ClientItem

        return [
            ClientItem(client_id=j.id, name=j.title, status=j.status if j.status != "queued" else "queued", progress=j.progress, path=j.path, message=j.message)
            for j in JOBS.values()
        ]

    async def remove(self, client_id: str, delete_files: bool = True) -> None:
        job = JOBS.pop(client_id, None)
        if job and delete_files and job.path and Path(job.path).is_dir():
            shutil.rmtree(job.path, ignore_errors=True)
