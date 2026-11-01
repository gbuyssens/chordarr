"""Runtime preferences: a handful of settings the UI may change without a restart.

Environment / ``chordarr.toml`` give the default; a row in the ``settings`` table overrides it.
"""

from __future__ import annotations

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
