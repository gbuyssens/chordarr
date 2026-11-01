"""Cover Art Archive front covers, by release group."""

from __future__ import annotations

from chordarr.config import settings


def release_group_cover(rg_mbid: str, size: int = 500) -> str:
    """URL that redirects to the group's front cover; 404 when none exists. Cheap, no API call."""
    return f"{settings.coverart_url}/release-group/{rg_mbid}/front-{size}"
