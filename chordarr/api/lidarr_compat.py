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
