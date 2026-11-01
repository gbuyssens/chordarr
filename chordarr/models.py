"""ORM models.

Vocabulary
----------
* An **artist** is tracked by its MusicBrainz artist MBID.
* A **release** is a MusicBrainz *release group* (the abstract album/single/EP), not one of
  its physical editions. ``kind`` tells whether the tracked artist owns it (``own``) or
  merely appears on it (``appearance``: a feature, a remix credit, a compilation slot…).
* An **appearance** links an artist to the recording(s) through which it appears on a
  release it does not own. That is the information Lidarr never surfaces.
* **Events** form the timeline (new release found, appearance found, import done…) and are
  what notifiers consume.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(primary_key=True)
    mbid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    sort_name: Mapped[str] = mapped_column(String, default="")
    disambiguation: Mapped[str] = mapped_column(String, default="")
    type: Mapped[str] = mapped_column(String, default="")  # Person, Group…
    country: Mapped[str] = mapped_column(String, default="")
    deezer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    path: Mapped[str | None] = mapped_column(String, nullable=True)

    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    # Which of the artist's own release types to watch. JSON list of primary types.
    monitor_types: Mapped[list] = mapped_column(JSON, default=lambda: ["Album", "EP", "Single"])
    monitor_appearances: Mapped[bool] = mapped_column(Boolean, default=True)
    # Lidarr's "monitor new items": all | none. New releases found at refresh follow it.
    monitor_new_items: Mapped[str] = mapped_column(String, default="all")

    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_refresh: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refresh_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    releases: Mapped[list[Release]] = relationship(
        back_populates="artist", cascade="all, delete-orphan", lazy="selectin"
    )
    appearances: Mapped[list[Appearance]] = relationship(
        back_populates="artist", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Artist {self.name} {self.mbid}>"


class Release(Base):
    """A MusicBrainz release group as seen from one tracked artist."""

    __tablename__ = "releases"
    __table_args__ = (UniqueConstraint("artist_id", "mbid", name="uq_release_artist_mbid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), index=True)
    mbid: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String)
    artist_credit: Mapped[str] = mapped_column(String, default="")  # "Damso feat. Hamza"
    primary_type: Mapped[str] = mapped_column(String, default="Other")  # Album, Single, EP, Broadcast, Other
    secondary_types: Mapped[list] = mapped_column(JSON, default=list)  # Compilation, Live, Remix, Mixtape/Street…
    kind: Mapped[str] = mapped_column(String, default="own")  # own | appearance
    first_release_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_precision: Mapped[str] = mapped_column(String, default="day")  # day | month | year | none
    cover_url: Mapped[str | None] = mapped_column(String, nullable=True)

    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String, default="missing")  # missing | have | partial | ignored
    path: Mapped[str | None] = mapped_column(String, nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    track_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_search_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    artist: Mapped[Artist] = relationship(back_populates="releases")
    appearances: Mapped[list[Appearance]] = relationship(
        back_populates="release", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def year(self) -> int | None:
        return self.first_release_date.year if self.first_release_date else None

    @property
    def is_upcoming(self) -> bool:
        return bool(self.first_release_date and self.first_release_date > date.today())

    @property
    def type_label(self) -> str:
        parts = [self.primary_type] + list(self.secondary_types or [])
        return " · ".join(p for p in parts if p)


class Appearance(Base):
    """One recording on which a tracked artist appears, inside a release it does not own."""

    __tablename__ = "appearances"
    __table_args__ = (UniqueConstraint("release_id", "recording_mbid", name="uq_appearance_rec"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), index=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id", ondelete="CASCADE"), index=True)
    recording_mbid: Mapped[str] = mapped_column(String(36))
    recording_title: Mapped[str] = mapped_column(String)
    artist_credit: Mapped[str] = mapped_column(String, default="")
    role: Mapped[str] = mapped_column(String, default="featured")  # featured | credited | remixer | other

    artist: Mapped[Artist] = relationship(back_populates="appearances")
    release: Mapped[Release] = relationship(back_populates="appearances")


class Event(Base):
    """Timeline entry. Notifiers are fed from here."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String, index=True)
    # artist_added | release_found | appearance_found | release_imported | release_upcoming
    # | release_grabbed | download_failed | import_failed | library_scanned | test | error
    artist_id: Mapped[int | None] = mapped_column(ForeignKey("artists.id", ondelete="SET NULL"), nullable=True)
    release_id: Mapped[int | None] = mapped_column(ForeignKey("releases.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Notifier(Base):
    __tablename__ = "notifiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String)  # webhook | discord | matrix | ntfy
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # Event types this notifier wants. Empty list = everything.
    events: Mapped[list] = mapped_column(JSON, default=list)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, index=True)
    mbid: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    deezer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    # {"because": [{"artist": "Damso", "source": "deezer"}, …]}
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    """Free-form key/value store for things changed from the UI."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


# --- acquisition (0.2) ----------------------------------------------------------------------


class Indexer(Base):
    """A Torznab or Newznab endpoint, usually one Prowlarr pushes through the Lidarr API."""

    __tablename__ = "indexers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    implementation: Mapped[str] = mapped_column(String, default="Torznab")  # Torznab | Newznab
    enable_rss: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_automatic_search: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_interactive_search: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=25)  # 1 (first) … 50, like Lidarr

    base_url: Mapped[str] = mapped_column(String)
    api_path: Mapped[str] = mapped_column(String, default="/api")
    api_key: Mapped[str] = mapped_column(String, default="")
    categories: Mapped[list] = mapped_column(JSON, default=lambda: [3000, 3010, 3040])
    minimum_seeders: Mapped[int] = mapped_column(Integer, default=1)
    seed_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    seed_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discography_seed_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    early_release_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    additional_parameters: Mapped[str] = mapped_column(String, default="")
    reject_blocklisted_hashes: Mapped[bool] = mapped_column(Boolean, default=False)
    download_client_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    # Lidarr fields Chordarr has no column for, kept so that Prowlarr reads back what it wrote.
    extra_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    # From ``t=caps``: {"music_params": [...], "search_params": [...], "categories": {...}}
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)

    last_rss: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def protocol(self) -> str:
        return "usenet" if self.implementation == "Newznab" else "torrent"

    @property
    def api_url(self) -> str:
        return self.base_url.rstrip("/") + "/" + (self.api_path or "/api").strip("/")


class DownloadClient(Base):
    __tablename__ = "download_clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    implementation: Mapped[str] = mapped_column(String)  # qbittorrent | sabnzbd
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=1)  # lowest number wins among clients of a protocol
    url: Mapped[str] = mapped_column(String)  # http://host:port[/base]
    username: Mapped[str] = mapped_column(String, default="")
    password: Mapped[str] = mapped_column(String, default="")
    api_key: Mapped[str] = mapped_column(String, default="")  # SABnzbd
    category: Mapped[str] = mapped_column(String, default="chordarr")
    # Path the client reports → path Chordarr sees (empty when both run on the same machine).
    remote_path: Mapped[str] = mapped_column(String, default="")
    local_path: Mapped[str] = mapped_column(String, default="")
    remove_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def protocol(self) -> str:
        if self.implementation == "sabnzbd":
            return "usenet"
        if self.implementation == "streamrip":
            return "direct"
        return "torrent"


class Download(Base):
    """One grabbed release on its way through a download client: the queue."""

    __tablename__ = "downloads"

    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[int | None] = mapped_column(ForeignKey("releases.id", ondelete="SET NULL"), nullable=True, index=True)
    indexer_id: Mapped[int | None] = mapped_column(ForeignKey("indexers.id", ondelete="SET NULL"), nullable=True)
    download_client_id: Mapped[int | None] = mapped_column(ForeignKey("download_clients.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String)
    guid: Mapped[str] = mapped_column(String, default="")
    download_url: Mapped[str] = mapped_column(Text, default="")
    protocol: Mapped[str] = mapped_column(String, default="torrent")
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seeders: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality: Mapped[str] = mapped_column(String, default="unknown")
    client_id: Mapped[str | None] = mapped_column(String, nullable=True)  # torrent hash or SABnzbd nzo_id
    # grabbed | downloading | completed | importing | imported | failed | warning
    status: Mapped[str] = mapped_column(String, default="grabbed", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0)
    output_path: Mapped[str | None] = mapped_column(String, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    release: Mapped[Release | None] = relationship(lazy="selectin")

    @property
    def active(self) -> bool:
        return self.status in ("grabbed", "downloading", "completed", "importing")


class MediaServer(Base):
    __tablename__ = "media_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String)  # jellyfin | plex
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    url: Mapped[str] = mapped_column(String)
    api_key: Mapped[str] = mapped_column(String, default="")  # Jellyfin API key or Plex token
    section: Mapped[str] = mapped_column(String, default="")  # Plex library section id (optional)
    # Path Chordarr sees → path the media server sees (empty when identical).
    local_path: Mapped[str] = mapped_column(String, default="")
    remote_path: Mapped[str] = mapped_column(String, default="")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Tag(Base):
    """Lidarr-compatible tags; Prowlarr may attach them to the indexers it pushes."""

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String, unique=True)
