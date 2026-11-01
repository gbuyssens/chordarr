"""The Lidarr-compatible indexer API, driven the way Prowlarr drives it."""

import pytest
from httpx import ASGITransport, AsyncClient

from chordarr.services import indexers

HEADERS = {"X-Api-Key": "test-key"}
PROWLARR_URL = "http://prowlarr:9696"


def prowlarr_indexer(schema_entry: dict, indexer_id: int = 1, protocol: str = "torrent") -> dict:
    """What Prowlarr builds in BuildLidarrIndexer: schema fields filtered to the ones it syncs."""
    sync = {"baseUrl", "apiPath", "apiKey", "categories", "minimumSeeders", "seedCriteria.seedRatio", "seedCriteria.seedTime", "seedCriteria.discographySeedTime", "rejectBlocklistedTorrentHashesWhileGrabbing", "earlyReleaseLimit", "additionalParameters"}
    fields = [dict(f) for f in schema_entry["fields"] if f["name"] in sync]
    values = {"baseUrl": f"{PROWLARR_URL}/{indexer_id}/", "apiPath": "/api", "apiKey": "prowlarr-key", "categories": [3000, 3010, 3040]}
    if protocol == "torrent":
        values |= {"minimumSeeders": 1, "seedCriteria.seedRatio": None, "seedCriteria.seedTime": None, "seedCriteria.discographySeedTime": None, "rejectBlocklistedTorrentHashesWhileGrabbing": False}
    for f in fields:
        if f["name"] in values:
            f["value"] = values[f["name"]]
    return {
        "id": 0,
        "name": "TR4KER (Prowlarr)",
        "enableRss": True,
        "enableAutomaticSearch": True,
        "enableInteractiveSearch": True,
        "priority": 25,
        "implementation": schema_entry["implementation"],
        "configContract": schema_entry["configContract"],
        "fields": fields,
        "tags": [],
    }


@pytest.fixture
async def client(monkeypatch):
    async def fake_caps(indexer):
        return {"search_params": ["q"], "music_params": ["q"], "categories": {"3000": "Audio"}}

    monkeypatch.setattr(indexers, "caps", fake_caps)
    from chordarr.app import app
    from chordarr.db import init_db

    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_prowlarr_sync_cycle(client):
    # 1. connection test: 2xx and the version header Prowlarr checks (>= 1.0.2)
    r = await client.post("/api/v1/indexer/test", json={"implementation": "Newznab", "fields": [{"name": "baseUrl", "value": f"{PROWLARR_URL}/0/"}, {"name": "categories", "value": [3000]}]}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert tuple(int(x) for x in r.headers["X-Application-Version"].split(".")) >= (1, 0, 2)
    assert r.headers["X-Chordarr-Version"]

    # 2. schema: Prowlarr needs a Newznab and a Torznab entry carrying every synced field
    schemas = {s["implementation"]: s for s in (await client.get("/api/v1/indexer/schema", headers=HEADERS)).json()}
    assert set(schemas) == {"Newznab", "Torznab"}
    torznab_fields = {f["name"] for f in schemas["Torznab"]["fields"]}
    assert {"baseUrl", "apiPath", "apiKey", "categories", "minimumSeeders", "seedCriteria.seedRatio", "seedCriteria.seedTime", "seedCriteria.discographySeedTime", "rejectBlocklistedTorrentHashesWhileGrabbing", "earlyReleaseLimit", "additionalParameters"} <= torznab_fields
    assert schemas["Torznab"]["configContract"] == "TorznabSettings"

    # 3. GetIndexerMappings on an empty instance
    assert (await client.get("/api/v1/indexer", headers=HEADERS)).json() == []

    # 4. AddIndexer
    body = prowlarr_indexer(schemas["Torznab"])
    r = await client.post("/api/v1/indexer", json=body, headers=HEADERS)
    assert r.status_code == 201, r.text
    remote = r.json()
    rid = remote["id"]
    assert remote["name"] == "TR4KER (Prowlarr)" and remote["implementation"] == "Torznab" and remote["protocol"] == "torrent"
    fields = {f["name"]: f.get("value") for f in remote["fields"]}
    assert fields["baseUrl"] == f"{PROWLARR_URL}/1/" and fields["categories"] == [3000, 3010, 3040]
    assert fields["apiKey"] == "********"  # masked like Lidarr; Prowlarr's Equals accepts the mask
    assert fields["minimumSeeders"] == 1 and "seedCriteria.seedRatio" not in {k for k, v in fields.items() if v is not None}
    assert remote["capabilities"]["music_params"] == ["q"]

    # 5. GetIndexers → mapping by baseUrl prefix
    rows = (await client.get("/api/v1/indexer", headers=HEADERS)).json()
    assert [r["id"] for r in rows] == [rid]

    # 6. UpdateIndexer with the masked key keeps the stored key; a real key replaces it
    body = prowlarr_indexer(schemas["Torznab"]) | {"id": rid, "priority": 10, "enableRss": False}
    for f in body["fields"]:
        if f["name"] == "apiKey":
            f["value"] = "********"
        if f["name"] == "minimumSeeders":
            f["value"] = "3"  # Prowlarr may send numbers as strings
    r = await client.put(f"/api/v1/indexer/{rid}", json=body, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == 10 and r.json()["enableRss"] is False
    from sqlalchemy import select

    from chordarr.db import SessionLocal
    from chordarr.models import Indexer

    async with SessionLocal() as s:
        stored = await s.scalar(select(Indexer).where(Indexer.id == rid))
        assert stored.api_key == "prowlarr-key" and stored.minimum_seeders == 3 and stored.api_url == f"{PROWLARR_URL}/1/api"

    # 7. GetIndexer / RemoveIndexer
    assert (await client.get(f"/api/v1/indexer/{rid}", headers=HEADERS)).status_code == 200
    assert (await client.delete(f"/api/v1/indexer/{rid}", headers=HEADERS)).status_code == 200
    assert (await client.get(f"/api/v1/indexer/{rid}", headers=HEADERS)).status_code == 404

    # 8. Newznab entry and bad input
    r = await client.post("/api/v1/indexer", json=prowlarr_indexer(schemas["Newznab"], 2, "usenet"), headers=HEADERS)
    assert r.status_code == 201 and r.json()["protocol"] == "usenet"
    r = await client.post("/api/v1/indexer", json={"implementation": "Torznab", "fields": [{"name": "categories", "value": []}]}, headers=HEADERS)
    assert r.status_code == 400


async def test_tags_and_clients_and_mediaservers(client):
    r = await client.post("/api/v1/tag", json={"label": "Music"}, headers=HEADERS)
    assert r.status_code == 201 and r.json()["label"] == "music"
    assert (await client.get("/api/v1/tag", headers=HEADERS)).json()[0]["id"] == r.json()["id"]

    r = await client.post("/api/v1/downloadclient", json={"name": "qb", "implementation": "qbittorrent", "url": "http://qb:8080"}, headers=HEADERS)
    assert r.status_code == 201 and r.json()["category"] == "chordarr"
    r = await client.post("/api/v1/mediaserver", json={"name": "jf", "type": "jellyfin", "url": "http://jf:8096", "api_key": "k"}, headers=HEADERS)
    assert r.status_code == 201
    assert (await client.get("/api/v1/queue", headers=HEADERS)).json() == []

    r = await client.put("/api/v1/config/acquisition", json={"auto_grab": True, "quality_profile": "any"}, headers=HEADERS)
    assert r.json() == {"auto_grab": True, "quality_profile": "any"}
    r = await client.put("/api/v1/config/acquisition", json={"auto_grab": False}, headers=HEADERS)
    assert r.json()["auto_grab"] is False and r.json()["quality_profile"] == "any"

    for page in ("/queue", "/settings"):
        r = await client.get(page)
        assert r.status_code == 200, page
    assert "TR4KER (Prowlarr)" in (await client.get("/settings")).text  # pushed in the previous test, same database
