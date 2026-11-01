"""Runtime configuration.

Everything is read from environment variables prefixed with ``CHORDARR_`` and, when
present, from a ``chordarr.toml`` file in the data directory. The data directory itself
can only come from the environment (``CHORDARR_DATA_DIR``) or the ``--data`` flag.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import tomllib
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    env = os.environ.get("CHORDARR_DATA_DIR")
    if env:
        return Path(env)
    if os.geteuid() == 0 and Path("/var/lib").is_dir():
        return Path("/var/lib/chordarr")
    return Path.home() / ".config" / "chordarr"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHORDARR_", extra="ignore")

    data_dir: Path = Field(default_factory=default_data_dir)
    host: str = "0.0.0.0"
    port: int = 8688
    url_base: str = ""  # e.g. "/chordarr" when served under a sub-path
    log_level: str = "info"

    # Security. The API key protects /api; the UI is meant to sit behind a reverse proxy
    # with its own SSO (Authentik, Authelia…). Set ui_password to add a simple login.
    api_key: str = ""
    ui_password: str = ""
    auth_disabled_for_local: bool = True

    # Library
    root_folder: Path | None = None
    artist_folder_format: str = "{artist}"
    album_folder_format: str = "{album} ({year})"
    track_format: str = "{nn} - {title}"  # also {track}, {disc}, {artist}, {album}, {year}

    # Acquisition. Nothing is grabbed on its own until auto_grab is on; the UI can override
    # auto_grab and quality_profile at runtime (stored in the database).
    auto_grab: bool = False
    quality_profile: str = "lossless"  # lossless | high | any — the minimum accepted; best wins
    import_mode: str = "auto"  # auto (hardlink torrents, move usenet) | hardlink | copy | move
    search_interval_hours: float = 6
    rss_interval_minutes: float = 30
    queue_poll_seconds: float = 60

    # Upstream services
    musicbrainz_url: str = "https://musicbrainz.org/ws/2"
    musicbrainz_contact: str = "https://github.com/gbuyssens/chordarr"
    deezer_url: str = "https://api.deezer.com"
    listenbrainz_labs_url: str = "https://labs.api.listenbrainz.org"
    coverart_url: str = "https://coverartarchive.org"
    lastfm_api_key: str = ""

    # Scheduler (hours)
    refresh_interval_hours: float = 12
    scan_interval_hours: float = 6
    recommendations_interval_hours: float = 24

    @property
    def db_path(self) -> Path:
        return self.data_dir / "chordarr.db"

    @property
    def user_agent(self) -> str:
        from chordarr import __version__

        return f"Chordarr/{__version__} ( {self.musicbrainz_contact} )"


def _load_toml(data_dir: Path) -> dict:
    path = data_dir / "chordarr.toml"
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_settings() -> Settings:
    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    file_values = _load_toml(data_dir)
    # Environment wins over the file: build file values first, then let pydantic read env.
    settings = Settings(**{k: v for k, v in file_values.items() if k in Settings.model_fields})
    if not settings.api_key:
        settings.api_key = _ensure_api_key(data_dir)
    return settings


def _ensure_api_key(data_dir: Path) -> str:
    """Generate the API key once and persist it, like the other *arr apps do."""
    key_file = data_dir / "api_key"
    if key_file.is_file():
        key = key_file.read_text().strip()
        if key:
            return key
    key = secrets.token_hex(16)
    key_file.write_text(key + "\n")
    with contextlib.suppress(OSError):
        key_file.chmod(0o600)
    return key


settings: Settings = load_settings()
