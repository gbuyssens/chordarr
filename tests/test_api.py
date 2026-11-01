"""API smoke tests against a temporary database, MusicBrainz and Deezer stubbed out."""

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
            mb.ReleaseGroupInfo("rg-1", "Ipséité", "Album", [], mb.parse_partial_date("2017-04-28")[0], "day", "Damso", [mbid]),
            mb.ReleaseGroupInfo("rg-2", "Future", "Single", [], mb.parse_partial_date("2099-01-01")[0], "day", "Damso", [mbid]),
        ]

    async def fake_apps(mbid, max_items=3000):
        rg = mb.ReleaseGroupInfo("rg-3", "Nero Nemesis", "Album", [], mb.parse_partial_date("2015-12-04")[0], "day", "Booba", ["booba"])
        return [mb.AppearanceInfo(rg, [mb.TrackCredit("rec-1", "Pinocchio", "Booba feat. Damso & Gato", ["booba", mbid, "gato"])])]

    async def fake_deezer(name):
        return None

    monkeypatch.setattr(mb, "get_artist", fake_artist)
    monkeypatch.setattr(mb, "artist_release_groups", fake_groups)
    monkeypatch.setattr(mb, "artist_appearances", fake_apps)
    monkeypatch.setattr(deezer, "search_artist", fake_deezer)

    from chordarr.app import app
    from chordarr.db import init_db

    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_requires_api_key(client):
    r = await client.get("/api/v1/artist")
    assert r.status_code == 401
    r = await client.get("/api/v1/artist", headers=HEADERS)
    assert r.status_code == 200


async def test_add_artist_builds_catalogue(client):
    r = await client.post("/api/v1/artist", json={"mbid": DAMSO}, headers=HEADERS)
    assert r.status_code == 201, r.text
    artist = r.json()
    assert artist["name"] == "Damso"
    assert artist["release_count"] == 2
    assert artist["appearance_count"] == 1

    r = await client.post("/api/v1/artist", json={"mbid": DAMSO}, headers=HEADERS)
    assert r.status_code == 409

    rels = (await client.get(f"/api/v1/artist/{artist['id']}/release", headers=HEADERS)).json()
    kinds = {(x["kind"], x["title"]) for x in rels}
    assert ("appearance", "Nero Nemesis") in kinds

    apps = (await client.get(f"/api/v1/artist/{artist['id']}/appearance", headers=HEADERS)).json()
    assert apps[0]["role"] == "featured"
    assert apps[0]["recording_title"] == "Pinocchio"

    cal = (await client.get("/api/v1/calendar?start=2099-01-01&end=2099-12-31", headers=HEADERS)).json()
    assert [c["title"] for c in cal] == ["Future"]

    wanted = (await client.get("/api/v1/wanted", headers=HEADERS)).json()
    assert {w["title"] for w in wanted} == {"Ipséité", "Nero Nemesis"}

    stats = (await client.get("/api/v1/stats", headers=HEADERS)).json()
    assert stats["artists"] == 1 and stats["upcoming"] == 1

    page = await client.get(f"/artists/{artist['id']}")
    assert page.status_code == 200
    assert "Pinocchio" in page.text


async def test_notifier_crud_and_webhook_payload(client, monkeypatch):
    sent = {}
    import httpx

    original_post = httpx.AsyncClient.post

    async def fake_post(self, url, **kw):
        if not str(url).startswith("http://n8n"):
            return await original_post(self, url, **kw)
        sent["url"] = url
        sent["json"] = kw.get("json")

        class R:
            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    r = await client.post("/api/v1/notification", json={"name": "n8n", "type": "webhook", "config": {"url": "http://n8n/hook"}}, headers=HEADERS)
    assert r.status_code == 201
    nid = r.json()["id"]
    r = await client.post(f"/api/v1/notification/{nid}/test", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert sent["url"] == "http://n8n/hook"
    assert sent["json"]["event"] == "test" and sent["json"]["source"] == "chordarr"
