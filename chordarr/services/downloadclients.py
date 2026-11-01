"""Download clients: qBittorrent (Web API v2) and SABnzbd.

Each client exposes the same four verbs: ``test``, ``add``, ``items`` and ``remove``. Items
come back as :class:`ClientItem` with a normalised status so the queue code never looks
at client-specific states.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx

from chordarr.config import settings
from chordarr.models import DownloadClient

log = logging.getLogger(__name__)

CLIENT_TYPES = {
    "qbittorrent": {"label": "qBittorrent", "protocol": "torrent", "fields": ["url", "username", "password", "category"]},
    "sabnzbd": {"label": "SABnzbd", "protocol": "usenet", "fields": ["url", "api_key", "category"]},
}


@dataclass
class ClientItem:
    client_id: str
    name: str
    status: str  # queued | downloading | paused | completed | failed
    progress: float  # 0..1
    size: int | None = None
    path: str | None = None  # where the content is, as the client sees it
    eta: int | None = None
    message: str = ""


class ClientError(Exception):
    pass


# --- bencode (just enough to hash a torrent's info dictionary) --------------------------


def bdecode(data: bytes) -> object:
    def parse(i: int):
        c = data[i : i + 1]
        if c == b"i":
            j = data.index(b"e", i)
            return int(data[i + 1 : j]), j + 1
        if c == b"l":
            out, i = [], i + 1
            while data[i : i + 1] != b"e":
                v, i = parse(i)
                out.append(v)
            return out, i + 1
        if c == b"d":
            out, i = {}, i + 1
            while data[i : i + 1] != b"e":
                k, i = parse(i)
                v, i = parse(i)
                out[k] = v
            return out, i + 1
        if c.isdigit():
            j = data.index(b":", i)
            n = int(data[i:j])
            return data[j + 1 : j + 1 + n], j + 1 + n
        raise ValueError(f"bad bencode at {i}")

    value, end = parse(0)
    if end != len(data):
        raise ValueError("trailing data")
    return value


def bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, str):
        return bencode(value.encode())
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(k) + bencode(value[k]) for k in sorted(value)) + b"e"
    raise TypeError(type(value))


def torrent_info(data: bytes) -> tuple[str, str]:
    """(info hash, name) of a .torrent file."""
    meta = bdecode(data)
    if not isinstance(meta, dict) or b"info" not in meta:
        raise ValueError("not a torrent file")
    info = meta[b"info"]
    name = info.get(b"name", b"").decode("utf-8", "replace") if isinstance(info, dict) else ""
    return hashlib.sha1(bencode(info)).hexdigest(), name


def magnet_hash(url: str) -> str | None:
    m = re.search(r"xt=urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})", url)
    if not m:
        return None
    value = m.group(1)
    if len(value) == 32:
        import base64

        return base64.b32decode(value.upper()).hex()
    return value.lower()


# --- path mapping -----------------------------------------------------------------------


def map_path(client: DownloadClient, remote: str) -> Path:
    """Translate a path the client reported into one Chordarr can open."""
    if client.remote_path and client.local_path:
        remote_prefix = client.remote_path.rstrip("/")
        if remote == remote_prefix or remote.startswith(remote_prefix + "/"):
            rel = remote[len(remote_prefix) :].lstrip("/")
            return Path(client.local_path) / PurePosixPath(rel) if rel else Path(client.local_path)
    return Path(remote)


# --- qBittorrent ------------------------------------------------------------------------


class QBittorrent:
    DONE = {"uploading", "stalledUP", "queuedUP", "pausedUP", "stoppedUP", "forcedUP", "checkingUP"}
    FAILED = {"error", "missingFiles"}
    PAUSED = {"pausedDL", "stoppedDL"}

    def __init__(self, client: DownloadClient):
        self.cfg = client
        self.base = client.url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base, timeout=30, headers={"User-Agent": settings.user_agent, "Referer": self.base})
        self._logged_in = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self._http.aclose()

    async def _login(self) -> None:
        if not self.cfg.username:
            return
        r = await self._http.post("/api/v2/auth/login", data={"username": self.cfg.username, "password": self.cfg.password})
        if r.status_code != 200 or r.text.strip() != "Ok.":
            raise ClientError(f"qBittorrent login failed ({r.status_code} {r.text.strip()[:60]})")
        self._logged_in = True

    async def _call(self, method: str, path: str, **kw) -> httpx.Response:
        r = await self._http.request(method, path, **kw)
        if r.status_code == 403 and not self._logged_in:
            await self._login()
            r = await self._http.request(method, path, **kw)
        if r.status_code == 403:
            raise ClientError("qBittorrent refused the request (403): check username/password or the IP whitelist")
        r.raise_for_status()
        return r

    async def test(self) -> str:
        r = await self._call("GET", "/api/v2/app/version")
        version = r.text.strip()
        cats = (await self._call("GET", "/api/v2/torrents/categories")).json()
        if self.cfg.category and self.cfg.category not in cats:
            await self._call("POST", "/api/v2/torrents/createCategory", data={"category": self.cfg.category, "savePath": ""})
        return f"qBittorrent {version}"

    async def add(self, name: str, torrent: bytes | None = None, url: str | None = None) -> str:
        data = {"category": self.cfg.category or "", "autoTMM": "true"}
        if torrent:
            info_hash, _ = torrent_info(torrent)
            kw = {"data": data, "files": {"torrents": (f"{name}.torrent", torrent, "application/x-bittorrent")}}
        elif url:
            info_hash = magnet_hash(url) or ""
            kw = {"data": {**data, "urls": url}}
        else:
            raise ClientError("nothing to add")
        try:
            r = await self._call("POST", "/api/v2/torrents/add", **kw)
        except httpx.HTTPStatusError as exc:
            # 409: already in the client (qBittorrent 5.3+); that is fine for a re-grab.
            if exc.response.status_code == 409 and info_hash and await self._exists(info_hash):
                return info_hash
            raise
        return await self._added(r, info_hash)

    async def _added(self, r: httpx.Response, info_hash: str) -> str:
        """qBittorrent ≤ 5.2 answers ``Ok.``/``Fails.``; 5.3+ answers JSON with the hashes."""
        text = r.text.strip()
        if text in ("Ok.", ""):
            return info_hash
        if text.startswith("{"):
            data = r.json()
            ids = [h.lower() for h in data.get("added_torrent_ids") or []]
            if ids:
                return ids[0]
            if info_hash and await self._exists(info_hash):
                return info_hash  # already there (re-grab of the same torrent)
            raise ClientError(f"qBittorrent did not add the torrent: {text[:120]}")
        raise ClientError(f"qBittorrent refused the torrent: {text[:80]}")

    async def _exists(self, info_hash: str) -> bool:
        rows = (await self._call("GET", "/api/v2/torrents/info", params={"hashes": info_hash})).json()
        return any(t.get("hash", "").lower() == info_hash for t in rows)

    async def items(self) -> list[ClientItem]:
        params = {"category": self.cfg.category} if self.cfg.category else {}
        rows = (await self._call("GET", "/api/v2/torrents/info", params=params)).json()
        out = []
        for t in rows:
            state = t.get("state", "")
            if state in self.FAILED:
                status = "failed"
            elif state in self.DONE or float(t.get("progress", 0)) >= 1:
                status = "completed"
            elif state in self.PAUSED:
                status = "paused"
            elif state in ("queuedDL", "allocating", "checkingResumeData"):
                status = "queued"
            else:
                status = "downloading"
            out.append(
                ClientItem(
                    client_id=t["hash"].lower(),
                    name=t.get("name", ""),
                    status=status,
                    progress=float(t.get("progress", 0)),
                    size=int(t.get("size") or 0) or None,
                    path=t.get("content_path") or t.get("save_path"),
                    eta=int(t["eta"]) if t.get("eta") not in (None, 8640000) else None,
                    message=state,
                )
            )
        return out

    async def remove(self, client_id: str, delete_files: bool = False) -> None:
        await self._call("POST", "/api/v2/torrents/delete", data={"hashes": client_id, "deleteFiles": "true" if delete_files else "false"})


# --- SABnzbd ----------------------------------------------------------------------------


class SABnzbd:
    def __init__(self, client: DownloadClient):
        self.cfg = client
        self.base = client.url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base, timeout=30, headers={"User-Agent": settings.user_agent})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self._http.aclose()

    async def _api(self, mode: str, files=None, **params) -> dict:
        query = {"mode": mode, "output": "json", "apikey": self.cfg.api_key, **params}
        r = await self._http.post("/api", params=query, files=files) if files else await self._http.get("/api", params=query)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("status") is False:
            raise ClientError(f"SABnzbd: {data.get('error', 'unknown error')}")
        return data

    async def test(self) -> str:
        data = await self._api("version")
        return f"SABnzbd {data.get('version', '?')}"

    async def add(self, name: str, nzb: bytes | None = None, url: str | None = None) -> str:
        params = {"nzbname": name}
        if self.cfg.category:
            params["cat"] = self.cfg.category
        if nzb:
            data = await self._api("addfile", files={"name": (f"{name}.nzb", nzb, "application/x-nzb")}, **params)
        elif url:
            data = await self._api("addurl", name=url, **params)
        else:
            raise ClientError("nothing to add")
        ids = data.get("nzo_ids") or []
        if not ids:
            raise ClientError("SABnzbd accepted nothing")
        return ids[0]

    async def items(self) -> list[ClientItem]:
        out = []
        queue = (await self._api("queue")).get("queue", {})
        for s in queue.get("slots", []):
            status = s.get("status", "").lower()
            mapped = "paused" if status == "paused" else "queued" if status in ("queued", "grabbing") else "downloading"
            pct = float(s.get("percentage") or 0) / 100
            out.append(ClientItem(client_id=s["nzo_id"], name=s.get("filename", ""), status=mapped, progress=pct, size=int(float(s.get("mb") or 0) * 1024 * 1024) or None, message=status))
        history = (await self._api("history", limit=100, **({"category": self.cfg.category} if self.cfg.category else {}))).get("history", {})
        for s in history.get("slots", []):
            status = s.get("status", "").lower()
            if status == "completed":
                mapped, pct = "completed", 1.0
            elif status == "failed":
                mapped, pct = "failed", 0.0
            else:
                mapped, pct = "downloading", 0.95  # extracting, verifying, repairing…
            out.append(ClientItem(client_id=s["nzo_id"], name=s.get("name", ""), status=mapped, progress=pct, size=int(s.get("bytes") or 0) or None, path=s.get("storage") or None, message=s.get("fail_message") or status))
        return out

    async def remove(self, client_id: str, delete_files: bool = False) -> None:
        with contextlib.suppress(Exception):
            await self._api("queue", name="delete", value=client_id, del_files=1 if delete_files else 0)
        await self._api("history", name="delete", value=client_id, del_files=1 if delete_files else 0)


def make_client(client: DownloadClient) -> QBittorrent | SABnzbd:
    if client.implementation == "qbittorrent":
        return QBittorrent(client)
    if client.implementation == "sabnzbd":
        return SABnzbd(client)
    raise ClientError(f"unknown download client type {client.implementation}")


async def test(client: DownloadClient) -> str:
    async with make_client(client) as c:
        return await c.test()
