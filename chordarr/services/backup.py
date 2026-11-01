"""Backups of the data directory, like Lidarr's System → Backup.

A backup is a zip in ``<data dir>/Backups/<manual|scheduled|update>/`` holding a
consistent copy of the SQLite database (``VACUUM INTO``), the API key and the optional
``chordarr.toml``. Restore: stop Chordarr, unzip over the data directory, start.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path

from chordarr.config import settings

log = logging.getLogger(__name__)
KINDS = ("manual", "scheduled", "update")
RETENTION = {"manual": 20, "scheduled": 7, "update": 3}


def backup_dir(kind: str) -> Path:
    return settings.data_dir / "Backups" / kind


def _create_sync(kind: str) -> Path:
    if kind not in KINDS:
        raise ValueError(kind)
    folder = backup_dir(kind)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
    target = folder / f"chordarr_backup_v{_version()}_{stamp}.zip"
    tmp_db = folder / f".{stamp}.db"
    con = sqlite3.connect(settings.db_path)
    try:
        con.execute("VACUUM INTO ?", (str(tmp_db),))
    finally:
        con.close()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp_db, "chordarr.db")
        for name in ("api_key", "chordarr.toml"):
            p = settings.data_dir / name
            if p.is_file():
                z.write(p, name)
    tmp_db.unlink(missing_ok=True)
    _prune(kind)
    log.info("backup written: %s", target)
    return target


def _version() -> str:
    from chordarr import __version__

    return __version__


def _prune(kind: str) -> None:
    files = sorted(backup_dir(kind).glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[RETENTION.get(kind, 7) :]:
        old.unlink(missing_ok=True)


async def create(kind: str = "manual") -> Path:
    return await asyncio.to_thread(_create_sync, kind)


def list_backups() -> list[dict]:
    out = []
    for kind in KINDS:
        folder = backup_dir(kind)
        if not folder.is_dir():
            continue
        for p in folder.glob("*.zip"):
            st = p.stat()
            out.append({"id": zlib.crc32(p.name.encode()), "name": p.name, "path": f"/backup/{kind}/{p.name}", "type": kind, "size": st.st_size, "time": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
    out.sort(key=lambda b: b["time"], reverse=True)
    return out


def newest(kind: str) -> datetime | None:
    """Naive UTC time of the newest backup of that kind, or None."""
    folder = backup_dir(kind)
    files = list(folder.glob("*.zip")) if folder.is_dir() else []
    if not files:
        return None
    return datetime.fromtimestamp(max(p.stat().st_mtime for p in files), UTC).replace(tzinfo=None)


def find(kind: str, name: str) -> Path | None:
    if kind not in KINDS or "/" in name or not name.endswith(".zip"):
        return None
    p = backup_dir(kind) / name
    return p if p.is_file() else None
