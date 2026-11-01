"""iCal feed, at Lidarr's address: ``/feed/v1/calendar/<anything>.ics?apikey=…``."""

from __future__ import annotations

import hmac
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chordarr.config import settings
from chordarr.db import get_session
from chordarr.models import Artist, Release

router = APIRouter(include_in_schema=False)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


@router.get("/feed/v1/calendar/{name}.ics")
async def ical(
    name: str,
    apikey: str = Query(""),
    pastDays: int = 7,
    futureDays: int = 28,
    unmonitored: bool = False,
    session: AsyncSession = Depends(get_session),
):
    if not hmac.compare_digest(apikey, settings.api_key):
        raise HTTPException(401)
    today = date.today()
    q = select(Release).where(Release.first_release_date >= today - timedelta(days=pastDays), Release.first_release_date <= today + timedelta(days=futureDays))
    if not unmonitored:
        q = q.where(Release.monitored.is_(True))
    rows = (await session.scalars(q.order_by(Release.first_release_date))).all()
    artists = {a.id: a for a in (await session.scalars(select(Artist))).all()}
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Chordarr//Chordarr//EN", "X-WR-CALNAME:Chordarr"]
    for r in rows:
        d = r.first_release_date
        artist = artists.get(r.artist_id)
        who = artist.name if artist else r.artist_credit
        summary = f"{who} - {r.title}" if r.kind == "own" else f"{who} on {r.artist_credit} - {r.title}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:chordarr-{r.mbid}-{r.artist_id}@chordarr",
            f"DTSTART;VALUE=DATE:{d:%Y%m%d}",
            f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{_escape(summary)}",
            f"DESCRIPTION:{_escape(r.type_label + (' · appearance' if r.kind == 'appearance' else '') + ' · ' + r.status)}",
            f"URL:https://musicbrainz.org/release-group/{r.mbid}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines) + "\r\n", media_type="text/calendar; charset=utf-8", headers={"Content-Disposition": f"inline; filename={name}.ics"})
