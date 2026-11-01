"""qBittorrent and SABnzbd clients against httpx mock transports."""

import asyncio
import hashlib
import json

import httpx

from chordarr.models import DownloadClient
from chordarr.services import downloadclients as dc
from chordarr.services.downloadclients import bdecode, bencode, magnet_hash, torrent_info

TORRENT = bencode({b"announce": b"http://t/announce", b"info": {b"name": b"Damso - QALF", b"piece length": 16384, b"pieces": b"x" * 20, b"length": 1000}})


def test_bencode_roundtrip_and_infohash():
    assert bdecode(TORRENT)[b"info"][b"name"] == b"Damso - QALF"
    h, name = torrent_info(TORRENT)
    assert name == "Damso - QALF"
    assert h == hashlib.sha1(bencode(bdecode(TORRENT)[b"info"])).hexdigest()
    assert magnet_hash("magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=x") == "abcdef0123456789abcdef0123456789abcdef01"


def _mount(handler):
    transport = httpx.MockTransport(handler)
    original = dc.httpx.AsyncClient

    class Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    return Patched


def test_qbittorrent_add_login_and_items(monkeypatch):
    seen = {"logged": False, "adds": []}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/v2/auth/login":
            seen["logged"] = True
            return httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=abc; path=/"})
        if not seen["logged"]:
            return httpx.Response(403, text="Forbidden")
        if p == "/api/v2/app/version":
            return httpx.Response(200, text="v5.3.0")
        if p == "/api/v2/torrents/categories":
            return httpx.Response(200, json={"lidarr": {}})
        if p == "/api/v2/torrents/createCategory":
            seen["category"] = request.content.decode()
            return httpx.Response(200)
        if p == "/api/v2/torrents/add":
            seen["adds"].append(request.content)
            return httpx.Response(200, text="Ok.")
        if p == "/api/v2/torrents/info":
            assert request.url.params["category"] == "chordarr"
            return httpx.Response(200, json=[
                {"hash": "ABC", "name": "Damso - QALF", "state": "stalledUP", "progress": 1, "size": 10, "content_path": "/data/torrents/chordarr/Damso - QALF", "eta": 8640000},
                {"hash": "def", "name": "x", "state": "downloading", "progress": 0.4, "size": 10, "content_path": "/data/torrents/chordarr/x", "eta": 120},
                {"hash": "err", "name": "y", "state": "missingFiles", "progress": 0.1, "size": 10},
            ])
        if p == "/api/v2/torrents/delete":
            seen["deleted"] = request.content.decode()
            return httpx.Response(200)
        return httpx.Response(404)

    monkeypatch.setattr(dc.httpx, "AsyncClient", _mount(handler))
    cfg = DownloadClient(name="qb", implementation="qbittorrent", url="http://qb:8080", username="admin", password="pw", category="chordarr")

    async def run():
        async with dc.make_client(cfg) as c:
            assert await c.test() == "qBittorrent v5.3.0"
            assert "category=chordarr" in seen["category"]
            h = await c.add("Damso - QALF", torrent=TORRENT)
            assert h == torrent_info(TORRENT)[0]
            assert b"Damso - QALF.torrent" in seen["adds"][0] and b"chordarr" in seen["adds"][0]
            assert await c.add("m", url="magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01") == "abcdef0123456789abcdef0123456789abcdef01"
            items = await c.items()
            assert [(i.client_id, i.status, i.progress) for i in items] == [("abc", "completed", 1.0), ("def", "downloading", 0.4), ("err", "failed", 0.1)]
            assert items[0].path == "/data/torrents/chordarr/Damso - QALF" and items[0].eta is None and items[1].eta == 120
            await c.remove("abc", delete_files=True)
            assert "deleteFiles=true" in seen["deleted"]

    asyncio.run(run())
    assert seen["logged"]


def test_sabnzbd_add_and_items(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        mode = request.url.params["mode"]
        assert request.url.params["apikey"] == "sabkey"
        if mode == "version":
            return httpx.Response(200, json={"version": "4.3.2"})
        if mode == "addfile":
            assert request.url.params["cat"] == "chordarr" and b"x.nzb" in request.content
            return httpx.Response(200, json={"status": True, "nzo_ids": ["SABnzbd_nzo_1"]})
        if mode == "queue":
            return httpx.Response(200, json={"queue": {"slots": [{"nzo_id": "SABnzbd_nzo_1", "filename": "x", "status": "Downloading", "percentage": "40", "mb": "100"}]}})
        if mode == "history":
            return httpx.Response(200, json={"history": {"slots": [{"nzo_id": "SABnzbd_nzo_2", "name": "y", "status": "Completed", "storage": "/complete/y", "bytes": 5}, {"nzo_id": "SABnzbd_nzo_3", "name": "z", "status": "Failed", "fail_message": "crc"}]}})
        return httpx.Response(200, json={"status": False, "error": json.dumps(dict(request.url.params))})

    monkeypatch.setattr(dc.httpx, "AsyncClient", _mount(handler))
    cfg = DownloadClient(name="sab", implementation="sabnzbd", url="http://sab:8080", api_key="sabkey", category="chordarr")

    async def run():
        async with dc.make_client(cfg) as c:
            assert await c.test() == "SABnzbd 4.3.2"
            assert await c.add("x", nzb=b"<nzb/>") == "SABnzbd_nzo_1"
            items = await c.items()
            assert [(i.client_id, i.status) for i in items] == [("SABnzbd_nzo_1", "downloading"), ("SABnzbd_nzo_2", "completed"), ("SABnzbd_nzo_3", "failed")]
            assert items[1].path == "/complete/y" and items[2].message == "crc"

    asyncio.run(run())
