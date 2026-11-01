"""Lidarr-shaped endpoints, iCal feed, backups, monitor options."""

import pytest
from httpx import ASGITransport, AsyncClient

from chordarr.metadata import deezer
from chordarr.metadata import musicbrainz as mb

DAMSO = "71ff695e-896a-4043-aea4-93eadde08d09"
HEADERS = {"X-Api-Key": "test-key"}


@pytest.fixture
async def client(monkeypatch):
    async def fake_artist(mbid):
        return mb.ArtistInfo(mbid=mbid, name="Damso", sort_name="Damso", type="Person", country="BE")

    async def fake_groups(mbid, max_items=2000):
        return [
            mb.ReleaseGroupInfo("rg-a", "Batterie Faible", "Album", [], mb.parse_partial_date("2016-07-08")[0], "day", "Damso", [mbid]),
            mb.ReleaseGroupInfo("rg-b", "Ipséité", "Album", [], mb.parse_partial_date("2017-04-28")[0], "day", "Damso", [mbid]),
            mb.ReleaseGroupInfo("rg-c", "Next", "Album", [], mb.parse_partial_date("2099-01-01")[0], "day", "Damso", [mbid]),
            mb.ReleaseGroupInfo("rg-d", "Live", "Album", ["Live"], mb.parse_partial_date("2018-01-01")[0], "day", "Damso", [mbid]),
        ]

    async def fake_apps(mbid, max_items=3000):
        return []

    async def fake_deezer(name):
        return None

    monkeypatch.setattr(mb, "get_artist", fake_artist)
    monkeypatch.setattr(mb, "artist_release_groups", fake_groups)
    monkeypatch.setattr(mb, "artist_appearances", fake_apps)
    monkeypatch.setattr(deezer, "search_artist", fake_deezer)

    from chordarr.app import app
    from chordarr.db import init_db

    await init_db()
    from sqlalchemy import delete

    from chordarr.db import SessionLocal
    from chordarr.models import Artist, Event

    async with SessionLocal() as s:  # the data directory is shared by the whole test session
        await s.execute(delete(Artist))
        await s.execute(delete(Event))
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _add(client, **extra):
    r = await client.post("/api/v1/artist", json={"foreignArtistId": DAMSO, **extra}, headers=HEADERS)
    assert r.status_code == 201, r.text
    return r.json()


async def test_lidarr_body_and_superset(client):
    a = await _add(client, addOptions={"monitor": "future"}, monitorNewItems="none")
    assert a["artistName"] == "Damso" and a["name"] == "Damso"
    assert a["foreignArtistId"] == DAMSO and a["mbid"] == DAMSO
    assert a["monitorNewItems"] == "none"
    assert "statistics" in a and "qualityProfileId" in a

    albums = (await client.get(f"/api/v1/album?artistId={a['id']}&includeAllArtistAlbums=true", headers=HEADERS)).json()
    by_title = {x["title"]: x for x in albums}
    assert by_title["Next"]["monitored"] is True  # future
    assert by_title["Ipséité"]["monitored"] is False
    assert by_title["Live"]["monitored"] is False
    assert by_title["Next"]["foreignAlbumId"] == "rg-c" and by_title["Next"]["artist"]["artistName"] == "Damso"

    r = await client.put("/api/v1/album/monitor", json={"albumIds": [by_title["Ipséité"]["id"]], "monitored": True}, headers=HEADERS)
    assert r.status_code == 200 and r.json()[0]["monitored"] is True

    missing = (await client.get("/api/v1/wanted/missing?page=1&pageSize=10", headers=HEADERS)).json()
    assert missing["totalRecords"] == 1 and missing["records"][0]["title"] == "Ipséité"

    cal = (await client.get("/api/v1/calendar?start=2099-01-01T00:00:00Z&end=2099-12-31T00:00:00Z", headers=HEADERS)).json()
    assert cal[0]["title"] == "Next" and cal[0]["albumType"] == "Album" and cal[0]["kind"] == "own"

    hist = (await client.get("/api/v1/history?pageSize=5", headers=HEADERS)).json()
    assert hist["page"] == 1 and hist["records"]

    r = await client.post("/api/v1/command", json={"name": "RefreshArtist", "artistId": a["id"]}, headers=HEADERS)
    assert r.status_code == 201 and r.json()["name"] == "RefreshArtist"
    assert (await client.get("/api/v1/metadataprofile", headers=HEADERS)).json()[0]["name"] == "Standard"
    assert (await client.get("/api/v1/config/host", headers=HEADERS)).json()["port"] == 8585

    r = await client.put(f"/api/v1/artist/{a['id']}", json={**a, "monitored": False}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["monitored"] is False


async def test_monitor_modes(client):
    from chordarr.db import SessionLocal
    from chordarr.models import Artist
    from chordarr.services.artists import apply_monitor_mode

    a = await _add(client, monitor="latest")
    async with SessionLocal() as session:
        artist = await session.get(Artist, a["id"])
        await session.refresh(artist, ["releases"])
        mon = {r.title for r in artist.releases if r.monitored}
        assert mon == {"Next"}
        await apply_monitor_mode(session, artist, "first")
        await session.refresh(artist, ["releases"])
        assert {r.title for r in artist.releases if r.monitored} == {"Batterie Faible"}
        await apply_monitor_mode(session, artist, "none")
        await session.refresh(artist, ["releases"])
        assert not any(r.monitored for r in artist.releases)
        await session.commit()


async def test_ical_feed(client):
    await _add(client)
    r = await client.get("/feed/v1/calendar/Chordarr.ics")
    assert r.status_code == 401
    r = await client.get("/feed/v1/calendar/Chordarr.ics?apikey=test-key&futureDays=40000")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VEVENT" in r.text and "SUMMARY:Damso - Next" in r.text


async def test_backup_roundtrip(client):
    r = await client.post("/api/v1/system/backup", headers=HEADERS)
    assert r.status_code == 201
    name = r.json()["name"]
    listing = (await client.get("/api/v1/system/backup", headers=HEADERS)).json()
    assert any(b["name"] == name for b in listing)
    r = await client.get(f"/backup/manual/{name}")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    bid = next(b["id"] for b in listing if b["name"] == name)
    assert (await client.delete(f"/api/v1/system/backup/{bid}", headers=HEADERS)).status_code == 204
