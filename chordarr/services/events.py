"""Timeline events."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from chordarr.models import Artist, Event, Release

EVENT_TYPES = {
    "artist_added": "Artist added",
    "release_found": "New release found",
    "appearance_found": "New appearance found",
    "release_upcoming": "Release coming up",
    "release_grabbed": "Release grabbed",
    "release_imported": "Release imported",
    "download_failed": "Download failed",
    "import_failed": "Import failed",
    "library_scanned": "Library scanned",
    "test": "Test",
    "error": "Error",
}


def add_event(
    session: AsyncSession,
    type: str,
    title: str,
    body: str = "",
    artist: Artist | None = None,
    release: Release | None = None,
    **payload,
) -> Event:
    data = dict(payload)
    if artist:
        data.setdefault("artist", {"id": artist.id, "mbid": artist.mbid, "name": artist.name})
    if release:
        data.setdefault(
            "release",
            {
                "id": release.id,
                "mbid": release.mbid,
                "title": release.title,
                "type": release.type_label,
                "kind": release.kind,
                "artist_credit": release.artist_credit,
                "date": release.first_release_date.isoformat() if release.first_release_date else None,
            },
        )
    ev = Event(
        type=type,
        title=title,
        body=body,
        payload=data,
        artist_id=artist.id if artist else None,
        release_id=release.id if release else None,
    )
    session.add(ev)
    return ev
