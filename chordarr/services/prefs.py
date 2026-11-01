"""Runtime preferences: a handful of settings the UI may change without a restart.

Environment / ``chordarr.toml`` give the default; a row in the ``settings`` table overrides it.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from chordarr.config import settings
from chordarr.models import Setting

QUALITY_PROFILES = {
    "lossless": "Lossless only (FLAC, ALAC, WAV…)",
    "high": "Lossless or high-quality lossy (320 kbps, V0)",
    "any": "Anything, best available first",
}


@dataclass
class Prefs:
    auto_grab: bool
    quality_profile: str


async def get_prefs(session: AsyncSession) -> Prefs:
    row = await session.get(Setting, "acquisition")
    values = dict(row.value) if row and row.value else {}
    profile = values.get("quality_profile", settings.quality_profile)
    if profile not in QUALITY_PROFILES:
        profile = "lossless"
    return Prefs(auto_grab=bool(values.get("auto_grab", settings.auto_grab)), quality_profile=profile)


async def set_prefs(session: AsyncSession, **values) -> Prefs:
    row = await session.get(Setting, "acquisition")
    if row is None:
        row = Setting(key="acquisition", value={})
        session.add(row)
    row.value = {**(row.value or {}), **values}
    await session.flush()
    return await get_prefs(session)


# --- settings the UI may override (stored in the "config" row, applied onto `settings`) ---

OVERRIDABLE = (
    "artist_folder_format",
    "album_folder_format",
    "track_format",
    "import_mode",
    "refresh_interval_hours",
    "scan_interval_hours",
    "recommendations_interval_hours",
    "rss_interval_minutes",
    "search_interval_hours",
    "queue_poll_seconds",
    "backup_interval_days",
    "log_level",
)


def _coerce(key: str, value):
    current = getattr(settings, key)
    if isinstance(current, bool):
        return value in (True, 1, "1", "true", "on", "yes")
    if isinstance(current, float):
        return float(value)
    if isinstance(current, int):
        return int(value)
    return str(value)


async def load_overrides(session: AsyncSession) -> dict:
    """Apply the stored overrides onto the live settings object (called at startup)."""
    row = await session.get(Setting, "config")
    values = dict(row.value) if row and row.value else {}
    for key, value in values.items():
        if key in OVERRIDABLE:
            with contextlib.suppress(TypeError, ValueError):
                setattr(settings, key, _coerce(key, value))
    return values


async def save_overrides(session: AsyncSession, **values) -> dict:
    row = await session.get(Setting, "config")
    if row is None:
        row = Setting(key="config", value={})
        session.add(row)
    clean = {}
    for key, value in values.items():
        if key in OVERRIDABLE and value is not None:
            clean[key] = _coerce(key, value)
            setattr(settings, key, clean[key])
    row.value = {**(row.value or {}), **clean}
    await session.flush()
    if "log_level" in clean:
        import logging

        logging.getLogger().setLevel(clean["log_level"].upper())
    return dict(row.value)
