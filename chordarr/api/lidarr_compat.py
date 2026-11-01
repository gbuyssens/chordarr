"""Lidarr-shaped indexer resources, so that Prowlarr manages Chordarr as a "Lidarr" application.

Prowlarr reads ``GET /api/v1/indexer/schema`` to find the ``Torznab`` and ``Newznab``
definitions, fills the fields it knows (``baseUrl``, ``apiPath``, ``apiKey``, ``categories``,
``minimumSeeders``, ``seedCriteria.*``…) and pushes the result with ``POST``/``PUT``
``/api/v1/indexer``. Its connection test is ``POST /api/v1/indexer/test`` and it expects an
``X-Application-Version`` header of at least 1.0.2 on the answer. Everything here converts
between that shape and :class:`chordarr.models.Indexer`.
"""

from __future__ import annotations

from typing import Any

from chordarr.models import Indexer

# What Prowlarr's version check reads. This is the Lidarr API level Chordarr speaks, not
# Chordarr's own version (sent as X-Chordarr-Version).
LIDARR_API_VERSION = "1.0.2.0"  # four parts: .NET parses "1.0.2" as 1.0.2.-1, below 1.0.2.0
MASK = "********"

_COMMON_FIELDS: list[dict] = [
    {"order": 0, "name": "baseUrl", "label": "URL", "type": "textbox", "advanced": False, "privacy": "normal"},
    {"order": 1, "name": "apiPath", "label": "API Path", "helpText": "Path to the api, usually /api", "value": "/api", "type": "textbox", "advanced": True, "privacy": "normal"},
    {"order": 2, "name": "apiKey", "label": "API Key", "type": "textbox", "advanced": False, "privacy": "apiKey"},
    {"order": 3, "name": "categories", "label": "Categories", "helpText": "Drop down list; at least one category must be selected.", "value": [3000, 3010, 3040], "type": "select", "advanced": False, "selectOptionsProviderAction": "newznabCategories", "privacy": "normal"},
    {"order": 4, "name": "earlyReleaseLimit", "label": "Early Download Limit", "unit": "days", "helpText": "Time before release date Chordarr will download from this indexer, empty is no limit", "type": "number", "advanced": True, "privacy": "normal"},
    {"order": 5, "name": "additionalParameters", "label": "Additional Parameters", "helpText": "Additional Newznab parameters", "type": "textbox", "advanced": True, "privacy": "normal"},
]
_TORRENT_FIELDS: list[dict] = [
    {"order": 6, "name": "minimumSeeders", "label": "Minimum Seeders", "helpText": "Minimum number of seeders required.", "value": 1, "type": "textbox", "advanced": True, "privacy": "normal"},
    {"order": 7, "name": "seedCriteria.seedRatio", "label": "Seed Ratio", "helpText": "The ratio a torrent should reach before stopping, empty uses the download client's default.", "type": "number", "advanced": False, "privacy": "normal", "isFloat": True},
    {"order": 8, "name": "seedCriteria.seedTime", "label": "Seed Time", "unit": "minutes", "helpText": "The time a torrent should be seeded before stopping, empty uses the download client's default", "type": "number", "advanced": True, "privacy": "normal"},
    {"order": 9, "name": "seedCriteria.discographySeedTime", "label": "Discography Seed Time", "unit": "minutes", "helpText": "The time a torrent should be seeded before stopping, empty is download client's default", "type": "textbox", "advanced": True, "privacy": "normal"},
    {"order": 10, "name": "rejectBlocklistedTorrentHashesWhileGrabbing", "label": "Reject Blocklisted Torrent Hashes While Grabbing", "helpText": "If a torrent is blocked by hash it may not properly be rejected during RSS/Search for some indexers, enabling this will allow it to be rejected after the torrent is grabbed, but before it is sent to the client.", "value": False, "type": "checkbox", "advanced": True, "privacy": "normal"},
]

IMPLEMENTATIONS = {
    "Torznab": {"configContract": "TorznabSettings", "protocol": "torrent", "fields": _COMMON_FIELDS + _TORRENT_FIELDS, "infoLink": "https://wiki.servarr.com/lidarr/supported#torznab"},
    "Newznab": {"configContract": "NewznabSettings", "protocol": "usenet", "fields": _COMMON_FIELDS, "infoLink": "https://wiki.servarr.com/lidarr/supported#newznab"},
}

# Lidarr field name → Indexer column.
FIELD_MAP = {
    "baseUrl": "base_url",
    "apiPath": "api_path",
    "apiKey": "api_key",
    "categories": "categories",
    "earlyReleaseLimit": "early_release_limit",
    "additionalParameters": "additional_parameters",
    "minimumSeeders": "minimum_seeders",
    "seedCriteria.seedRatio": "seed_ratio",
    "seedCriteria.seedTime": "seed_time",
    "seedCriteria.discographySeedTime": "discography_seed_time",
    "rejectBlocklistedTorrentHashesWhileGrabbing": "reject_blocklisted_hashes",
}


def schema() -> list[dict]:
    out = []
    for name, spec in IMPLEMENTATIONS.items():
        out.append(
            {
                "enableRss": True,
                "enableAutomaticSearch": True,
                "enableInteractiveSearch": True,
                "supportsRss": True,
                "supportsSearch": True,
                "protocol": spec["protocol"],
                "priority": 25,
                "downloadClientId": 0,
                "name": None,
                "fields": [dict(f) for f in spec["fields"]],
                "implementationName": name,
                "implementation": name,
                "configContract": spec["configContract"],
                "infoLink": spec["infoLink"],
                "tags": [],
                "presets": [],
            }
        )
    return out


def _coerce(name: str, value: Any) -> Any:
    """Prowlarr sends numbers as numbers or strings; store them typed."""
    if value in ("", None):
        return None if name not in ("additionalParameters", "apiKey", "apiPath") else ""
    if name == "categories":
        return [int(v) for v in value]
    if name in ("minimumSeeders", "seedCriteria.seedTime", "seedCriteria.discographySeedTime", "earlyReleaseLimit"):
        return int(float(value))
    if name == "seedCriteria.seedRatio":
        return float(value)
    if name == "rejectBlocklistedTorrentHashesWhileGrabbing":
        return bool(value) if not isinstance(value, str) else value.lower() == "true"
    return value


def apply(indexer: Indexer, body: dict) -> Indexer:
    """Copy a Lidarr indexer resource onto a model (new or existing)."""
    impl = body.get("implementation") or indexer.implementation or "Torznab"
    if impl not in IMPLEMENTATIONS:
        raise ValueError(f"unsupported implementation {impl!r}; Chordarr speaks Torznab and Newznab")
    indexer.implementation = impl
    indexer.name = body.get("name") or indexer.name or impl
    for attr, key in (("enable_rss", "enableRss"), ("enable_automatic_search", "enableAutomaticSearch"), ("enable_interactive_search", "enableInteractiveSearch")):
        if key in body:
            setattr(indexer, attr, bool(body[key]))
    if "priority" in body and body["priority"] is not None:
        indexer.priority = int(body["priority"])
    if "downloadClientId" in body:
        indexer.download_client_id = int(body["downloadClientId"]) or None
    if "tags" in body:
        indexer.tags = [int(t) for t in body["tags"] or []]
    extra = dict(indexer.extra_fields or {})
    for f in body.get("fields") or []:
        name, value = f.get("name"), f.get("value")
        if name == "apiKey" and value == MASK:
            continue
        column = FIELD_MAP.get(name)
        if column:
            setattr(indexer, column, _coerce(name, value))
        elif name:
            extra[name] = value
    indexer.extra_fields = extra
    if not indexer.base_url:
        raise ValueError("baseUrl is required")
    if not indexer.categories:
        raise ValueError("at least one category is required")
    if indexer.minimum_seeders is None:
        indexer.minimum_seeders = 0
    if indexer.api_path is None:
        indexer.api_path = "/api"
    return indexer


def to_resource(indexer: Indexer, mask_key: bool = True) -> dict:
    spec = IMPLEMENTATIONS.get(indexer.implementation, IMPLEMENTATIONS["Torznab"])
    fields = []
    for f in spec["fields"]:
        item = dict(f)
        column = FIELD_MAP.get(f["name"])
        value = getattr(indexer, column) if column else (indexer.extra_fields or {}).get(f["name"])
        if f["name"] == "apiKey" and mask_key and value:
            value = MASK
        if value is None:
            item.pop("value", None)
        else:
            item["value"] = value
        fields.append(item)
    known = {f["name"] for f in spec["fields"]}
    for name, value in (indexer.extra_fields or {}).items():
        if name not in known:
            fields.append({"order": len(fields), "name": name, "label": name, "value": value, "type": "textbox", "advanced": True, "privacy": "normal"})
    return {
        "id": indexer.id,
        "name": indexer.name,
        "enableRss": indexer.enable_rss,
        "enableAutomaticSearch": indexer.enable_automatic_search,
        "enableInteractiveSearch": indexer.enable_interactive_search,
        "supportsRss": True,
        "supportsSearch": True,
        "protocol": spec["protocol"],
        "priority": indexer.priority,
        "downloadClientId": indexer.download_client_id or 0,
        "implementationName": indexer.implementation,
        "implementation": indexer.implementation,
        "configContract": spec["configContract"],
        "infoLink": spec["infoLink"],
        "tags": list(indexer.tags or []),
        "fields": fields,
        # Chordarr extras
        "capabilities": indexer.capabilities or {},
        "lastError": indexer.last_error,
        "lastRss": indexer.last_rss.isoformat(timespec="seconds") if indexer.last_rss else None,
    }


# --- download clients -------------------------------------------------------------------
#
# Lidarr: {"name", "enable", "protocol", "priority", "removeCompletedDownloads",
# "removeFailedDownloads", "implementation": "QBittorrent" | "Sabnzbd", "configContract",
# "fields": [{"name": "host"|"port"|"useSsl"|"urlBase"|"username"|"password"|"apiKey"|
# "musicCategory", "value"}], "tags"}. Chordarr stores one URL, so host/port/useSsl/urlBase
# are split and joined here. Remote path mappings are Chordarr extras on the resource.

from urllib.parse import urlsplit  # noqa: E402

CLIENT_IMPLEMENTATIONS = {
    # Lidarr implementation → (Chordarr implementation, configContract, protocol, field names)
    "QBittorrent": ("qbittorrent", "QBittorrentSettings", "torrent", ["host", "port", "useSsl", "urlBase", "username", "password", "musicCategory"]),
    "Sabnzbd": ("sabnzbd", "SabnzbdSettings", "usenet", ["host", "port", "useSsl", "urlBase", "apiKey", "username", "password", "musicCategory"]),
}
_CLIENT_BY_INTERNAL = {v[0]: k for k, v in CLIENT_IMPLEMENTATIONS.items()}
_CLIENT_FIELD_DEFS = {
    "host": {"label": "Host", "type": "textbox", "value": "localhost"},
    "port": {"label": "Port", "type": "textbox"},
    "useSsl": {"label": "Use SSL", "type": "checkbox", "value": False},
    "urlBase": {"label": "URL Base", "type": "textbox", "advanced": True},
    "username": {"label": "Username", "type": "textbox", "privacy": "userName"},
    "password": {"label": "Password", "type": "password", "privacy": "password"},
    "apiKey": {"label": "API Key", "type": "textbox", "privacy": "apiKey"},
    "musicCategory": {"label": "Category", "type": "textbox", "value": "chordarr"},
}


def split_url(url: str) -> dict:
    u = urlsplit(url if "://" in url else f"http://{url}")
    return {"host": u.hostname or "", "port": u.port or (443 if u.scheme == "https" else 80), "useSsl": u.scheme == "https", "urlBase": u.path.strip("/")}


def join_url(host: str, port, use_ssl: bool, url_base: str) -> str:
    scheme = "https" if use_ssl else "http"
    base = f"{scheme}://{host}:{int(port) if port else (443 if use_ssl else 80)}"
    return f"{base}/{url_base.strip('/')}" if url_base and url_base.strip("/") else base


def client_schema() -> list[dict]:
    out = []
    for impl, (internal, contract, protocol, names) in CLIENT_IMPLEMENTATIONS.items():
        fields = []
        for i, name in enumerate(names):
            f = {"order": i, "name": name, "advanced": False, "privacy": "normal", **_CLIENT_FIELD_DEFS[name]}
            if name == "port":
                f["value"] = 8080  # qBittorrent and SABnzbd both default to 8080
            fields.append(f)
        out.append({"enable": True, "protocol": protocol, "priority": 1, "removeCompletedDownloads": False, "removeFailedDownloads": True, "name": None, "fields": fields, "implementationName": impl, "implementation": impl, "configContract": contract, "infoLink": f"https://wiki.servarr.com/lidarr/supported#{internal}", "tags": []})
    return out


def apply_client(client, body: dict):
    """Copy a Lidarr download client resource onto a :class:`DownloadClient`."""
    impl = body.get("implementation") or _CLIENT_BY_INTERNAL.get(client.implementation or "", "")
    if impl not in CLIENT_IMPLEMENTATIONS:
        raise ValueError(f"unsupported download client {impl!r}; Chordarr speaks QBittorrent and Sabnzbd")
    internal, _contract, _protocol, _names = CLIENT_IMPLEMENTATIONS[impl]
    client.implementation = internal
    client.name = body.get("name") or client.name or impl
    if "enable" in body:
        client.enabled = bool(body["enable"])
    if body.get("priority") is not None:
        client.priority = int(body["priority"])
    if "removeCompletedDownloads" in body:
        client.remove_completed = bool(body["removeCompletedDownloads"])
    for key in ("remotePath", "localPath"):
        if key in body:
            setattr(client, "remote_path" if key == "remotePath" else "local_path", str(body[key] or ""))
    fields = {f.get("name"): f.get("value") for f in body.get("fields") or []}
    current = split_url(client.url) if client.url else {"host": "", "port": None, "useSsl": False, "urlBase": ""}
    host = fields.get("host", current["host"])
    port = fields.get("port", current["port"])
    use_ssl = fields.get("useSsl", current["useSsl"])
    url_base = fields.get("urlBase", current["urlBase"]) or ""
    if not host:
        raise ValueError("host is required")
    client.url = join_url(str(host), port, bool(use_ssl), str(url_base))
    for name, attr in (("username", "username"), ("musicCategory", "category")):
        if name in fields and fields[name] is not None:
            setattr(client, attr, str(fields[name]))
    # Secrets come back masked when a client echoes what it read: keep the stored value then.
    for name, attr in (("password", "password"), ("apiKey", "api_key")):
        if name in fields and fields[name] not in (None, MASK):
            setattr(client, attr, str(fields[name]))
    extra = dict(client.settings or {})
    known = {"host", "port", "useSsl", "urlBase", "username", "password", "apiKey", "musicCategory"}
    extra.update({k: v for k, v in fields.items() if k not in known and k})
    if "removeFailedDownloads" in body:
        extra["removeFailedDownloads"] = bool(body["removeFailedDownloads"])
    client.settings = extra
    return client


def client_to_resource(client) -> dict:
    impl = _CLIENT_BY_INTERNAL.get(client.implementation, "QBittorrent")
    _internal, contract, protocol, names = CLIENT_IMPLEMENTATIONS[impl]
    parts = split_url(client.url)
    values = {
        "host": parts["host"],
        "port": parts["port"],
        "useSsl": parts["useSsl"],
        "urlBase": parts["urlBase"],
        "username": client.username,
        "password": MASK if client.password else "",
        "apiKey": MASK if client.api_key else "",
        "musicCategory": client.category,
    }
    fields = [{"order": i, "name": n, "advanced": False, "privacy": "normal", **{k: v for k, v in _CLIENT_FIELD_DEFS[n].items() if k != "value"}, "value": values[n]} for i, n in enumerate(names)]
    for name, value in (client.settings or {}).items():
        if name != "removeFailedDownloads":
            fields.append({"order": len(fields), "name": name, "label": name, "value": value, "type": "textbox", "advanced": True, "privacy": "normal"})
    return {
        "id": client.id,
        "name": client.name,
        "enable": client.enabled,
        "protocol": protocol,
        "priority": client.priority,
        "removeCompletedDownloads": client.remove_completed,
        "removeFailedDownloads": bool((client.settings or {}).get("removeFailedDownloads", True)),
        "implementationName": impl,
        "implementation": impl,
        "configContract": contract,
        "infoLink": f"https://wiki.servarr.com/lidarr/supported#{client.implementation}",
        "fields": fields,
        "tags": [],
        # Chordarr extras (Lidarr keeps these in /remotepathmapping)
        "remotePath": client.remote_path,
        "localPath": client.local_path,
        "lastError": client.last_error,
    }


# --- queue ------------------------------------------------------------------------------

_QUEUE_STATES = {
    # Chordarr status → (Lidarr status, trackedDownloadStatus, trackedDownloadState)
    "grabbed": ("queued", "ok", "downloading"),
    "downloading": ("downloading", "ok", "downloading"),
    "completed": ("completed", "ok", "importPending"),
    "importing": ("completed", "ok", "importing"),
    "imported": ("completed", "ok", "imported"),
    "failed": ("failed", "error", "failedPending"),
    "warning": ("completed", "warning", "importPending"),
}


def queue_record(d, indexer_name: str | None = None, client_name: str | None = None) -> dict:
    status, tracked, state = _QUEUE_STATES.get(d.status, ("unknown", "warning", "downloading"))
    size = d.size or 0
    return {
        "id": d.id,
        "artistId": d.release.artist_id if d.release else None,
        "albumId": d.release_id,
        "title": d.title,
        "quality": {"quality": {"name": d.quality}},
        "size": size,
        "sizeleft": int(size * (1 - (d.progress or 0))),
        "status": status,
        "trackedDownloadStatus": tracked,
        "trackedDownloadState": state,
        "statusMessages": [{"title": d.title, "messages": [d.message]}] if d.message and d.status in ("warning", "failed") else [],
        "errorMessage": d.message if d.status == "failed" else None,
        "downloadId": d.client_id,
        "protocol": d.protocol,
        "downloadClient": client_name,
        "downloadClientId": d.download_client_id,
        "indexer": indexer_name,
        "indexerId": d.indexer_id,
        "outputPath": d.output_path,
        "added": d.added_at.isoformat(timespec="seconds") if d.added_at else None,
        # Chordarr extras
        "progress": d.progress,
        "guid": d.guid,
        "seeders": d.seeders,
        "message": d.message,
        "importedAt": d.imported_at.isoformat(timespec="seconds") if d.imported_at else None,
    }


def page_envelope(records: list, page: int, page_size: int, sort_key: str, sort_dir: str = "descending", total: int | None = None) -> dict:
    """Lidarr's paged resource shape around an already-sliced list."""
    return {"page": page, "pageSize": page_size, "sortKey": sort_key, "sortDirection": sort_dir, "totalRecords": total if total is not None else len(records), "records": records}


def paged(records: list[dict], page: int = 1, page_size: int = 50, sort_key: str = "added") -> dict:
    """Slice ``records`` to one page and wrap it."""
    return page_envelope(records[(page - 1) * page_size : page * page_size], page, page_size, sort_key, total=len(records))
