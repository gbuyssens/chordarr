"""Notifiers: generic webhook, Discord, Matrix, ntfy.

Every event is dispatched once to each enabled notifier whose event filter accepts it. The
webhook payload is stable JSON so that n8n, Home Assistant or anything else can consume it:

    {"event": "release_found", "title": "...", "body": "...", "artist": {...}, "release": {...},
     "created_at": "2026-11-01T17:00:00", "source": "chordarr"}
"""

from __future__ import annotations

import logging
import time
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chordarr import __version__
from chordarr.config import settings
from chordarr.models import Event, Notifier, utcnow

log = logging.getLogger(__name__)

NOTIFIER_TYPES = {
    "webhook": {"label": "Webhook (JSON POST)", "fields": ["url", "secret"]},
    "discord": {"label": "Discord", "fields": ["webhook_url"]},
    "matrix": {"label": "Matrix", "fields": ["homeserver", "access_token", "room_id"]},
    "ntfy": {"label": "ntfy", "fields": ["url", "topic", "token"]},
}


def payload_for(event: Event) -> dict:
    return {
        "source": "chordarr",
        "version": __version__,
        "event": event.type,
        "title": event.title,
        "body": event.body,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        **event.payload,
    }


async def _send_webhook(cfg: dict, event: Event, client: httpx.AsyncClient) -> None:
    headers = {"Content-Type": "application/json", "X-Chordarr-Event": event.type}
    if cfg.get("secret"):
        headers["Authorization"] = f"Bearer {cfg['secret']}"
    r = await client.post(cfg["url"], json=payload_for(event), headers=headers)
    r.raise_for_status()


async def _send_discord(cfg: dict, event: Event, client: httpx.AsyncClient) -> None:
    rel = event.payload.get("release") or {}
    embed = {
        "title": event.title[:256],
        "description": event.body[:2000],
        "color": 0x6C5CE7 if event.type != "error" else 0xD63031,
        "footer": {"text": f"Chordarr · {event.type}"},
    }
    if rel.get("mbid"):
        embed["thumbnail"] = {"url": f"{settings.coverart_url}/release-group/{rel['mbid']}/front-250"}
    r = await client.post(cfg["webhook_url"], json={"username": "Chordarr", "embeds": [embed]})
    r.raise_for_status()


async def _send_matrix(cfg: dict, event: Event, client: httpx.AsyncClient) -> None:
    import html

    body = f"{event.title}\n{event.body}".strip()
    formatted = f"<strong>{html.escape(event.title)}</strong><br>{html.escape(event.body)}"
    txn = f"chordarr-{event.id}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
    url = f"{cfg['homeserver'].rstrip('/')}/_matrix/client/v3/rooms/{cfg['room_id']}/send/m.room.message/{txn}"
    r = await client.put(
        url,
        json={"msgtype": "m.notice", "body": body, "format": "org.matrix.custom.html", "formatted_body": formatted},
        headers={"Authorization": f"Bearer {cfg['access_token']}"},
    )
    r.raise_for_status()


async def _send_ntfy(cfg: dict, event: Event, client: httpx.AsyncClient) -> None:
    headers = {"Title": event.title.encode("ascii", "ignore").decode(), "Tags": "musical_note"}
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
    r = await client.post(f"{cfg.get('url', 'https://ntfy.sh').rstrip('/')}/{cfg['topic']}", content=event.body or event.title, headers=headers)
    r.raise_for_status()


SENDERS = {"webhook": _send_webhook, "discord": _send_discord, "matrix": _send_matrix, "ntfy": _send_ntfy}


async def send(notifier: Notifier, event: Event) -> None:
    sender = SENDERS[notifier.type]
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": settings.user_agent}) as client:
        await sender(notifier.config, event, client)


def accepts(notifier: Notifier, event: Event) -> bool:
    return notifier.enabled and (not notifier.events or event.type in notifier.events)


async def dispatch_pending(session: AsyncSession) -> int:
    notifiers = (await session.scalars(select(Notifier).where(Notifier.enabled.is_(True)))).all()
    pending = (await session.scalars(select(Event).where(Event.notified_at.is_(None)).order_by(Event.id).limit(200))).all()
    sent = 0
    for event in pending:
        for n in notifiers:
            if not accepts(n, event) or event.type in ("library_scanned",) and not n.events:
                continue
            try:
                await send(n, event)
                n.last_error = None
                sent += 1
            except Exception as exc:
                n.last_error = f"{utcnow():%Y-%m-%d %H:%M} {exc}"[:500]
                log.warning("notifier %s failed on event %s: %s", n.name, event.id, exc)
        event.notified_at = utcnow()
    await session.flush()
    return sent


async def test(notifier: Notifier) -> None:
    ev = Event(id=0, type="test", title="Chordarr test notification", body="If you can read this, the notifier works.", payload={}, created_at=utcnow())
    await send(notifier, ev)
