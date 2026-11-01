from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class ArtistIn(BaseModel):
    mbid: str = Field(alias="foreignArtistId", validation_alias="mbid")
    path: str | None = None
    monitored: bool = True
    monitor_types: list[str] = ["Album", "EP", "Single"]
    monitor_appearances: bool = True

    model_config = {"populate_by_name": True}


class ArtistPatch(BaseModel):
    monitored: bool | None = None
    monitor_types: list[str] | None = None
    monitor_appearances: bool | None = None
    path: str | None = None


class ArtistOut(BaseModel):
    id: int
    mbid: str
    name: str
    sort_name: str
    disambiguation: str
    type: str
    country: str
    image_url: str | None
    path: str | None
    monitored: bool
    monitor_types: list
    monitor_appearances: bool
    added_at: datetime
    last_refresh: datetime | None
    refresh_error: str | None
    release_count: int = 0
    appearance_count: int = 0
    have_count: int = 0

    model_config = {"from_attributes": True}


class ReleaseOut(BaseModel):
    id: int
    artist_id: int
    mbid: str
    title: str
    artist_credit: str
    primary_type: str
    secondary_types: list
    kind: str
    first_release_date: date | None
    date_precision: str
    cover_url: str | None
    monitored: bool
    status: str
    path: str | None
    file_count: int
    track_count: int | None

    model_config = {"from_attributes": True}


class ReleasePatch(BaseModel):
    monitored: bool | None = None
    status: str | None = None


class AppearanceOut(BaseModel):
    id: int
    release_id: int
    recording_mbid: str
    recording_title: str
    artist_credit: str
    role: str

    model_config = {"from_attributes": True}


class EventOut(BaseModel):
    id: int
    type: str
    title: str
    body: str
    payload: dict
    created_at: datetime
    notified_at: datetime | None

    model_config = {"from_attributes": True}


class NotifierIn(BaseModel):
    name: str
    type: str
    enabled: bool = True
    config: dict = {}
    events: list[str] = []


class NotifierOut(NotifierIn):
    id: int
    last_error: str | None = None

    model_config = {"from_attributes": True}


class RecommendationOut(BaseModel):
    id: int
    name: str
    mbid: str | None
    deezer_id: int | None
    image_url: str | None
    score: float
    reasons: list
    dismissed: bool

    model_config = {"from_attributes": True}


class LidarrImportIn(BaseModel):
    db_path: str | None = None
    url: str | None = None
    api_key: str | None = None
    only_monitored: bool = False


# --- acquisition (0.2) ------------------------------------------------------------------


class MediaServerIn(BaseModel):
    name: str
    type: str  # jellyfin | plex
    enabled: bool = True
    url: str
    api_key: str = ""
    section: str = ""
    local_path: str = ""
    remote_path: str = ""


class MediaServerOut(MediaServerIn):
    id: int
    last_error: str | None = None

    model_config = {"from_attributes": True}


class GrabIn(BaseModel):
    guid: str
    indexer_id: int | None = None


class QueueImportIn(BaseModel):
    path: str | None = None
    force: bool = False


class ManualImportIn(BaseModel):
    path: str
    release_id: int | None = None
    mode: str | None = None  # hardlink | copy | move
    force: bool = False


class AcquisitionConfigIn(BaseModel):
    auto_grab: bool | None = None
    quality_profile: str | None = None
