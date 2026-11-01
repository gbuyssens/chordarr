# Changelog

## 0.2.0 — 2026-11-01

Acquisition and the rest of Lidarr's surface.

- Indexers: Torznab and Newznab, with capabilities probing, music search (artist/album or
  text query), RSS. Prowlarr manages Chordarr as a **Lidarr** application without any
  change on its side: `/api/v1/indexer` (+ `/schema`, `/test`) and `/api/v1/tag` follow
  Lidarr's shapes, and answers carry `X-Application-Version`.
- Download clients: qBittorrent (Web API v2, JSON answers of 5.3+ included) and SABnzbd,
  dedicated category, remote path mappings. `/api/v1/downloadclient` is Lidarr-shaped.
- Search and grab: a candidate must name the artist and release, meet the quality
  profile (`lossless` > `high` > anything) and the indexer's minimum seeders; the best
  one wins. Interactive *Search* page per release; automatic RSS matching and periodic
  search only grab when `auto_grab` is on (off by default, switchable in the UI).
- Import: tags read with mutagen and checked against the expected release, files renamed
  to `{artist}/{album} ({year})/{nn} - {title}.ext` (`CHORDARR_TRACK_FORMAT`), hard-link
  for torrents, move for usenet, cover copied, `release_imported` event; manual import of
  a folder already on disk with release guessing from tags; *Activity → Queue* page.
- Media servers: Jellyfin/Emby and Plex, targeted scan of the imported folder with path
  mapping, test from the UI; `/api/v1/mediaserver`.
- New events `release_grabbed`, `download_failed`, `import_failed`; new tasks `rss`,
  `search`, `queue`; `GET /api/v1/queue` (paged, Lidarr record names),
  `/api/v1/qualityprofile`.
- Settings: *Indexers* and *Download Clients* pages, media servers under *Connect*,
  naming formats under *Media Management*.

- Lidarr-compatible API: `/api/v1/artist` and `/api/v1/calendar` carry Lidarr's field
  names next to Chordarr's; `/api/v1/album`, `/wanted/missing`, `/wanted/cutoff`,
  `/history`, `/history/since`, `/command`, `/metadataprofile`, `/config/*`,
  `/system/task`, `/update`, `/system/backup`. `POST /api/v1/artist` accepts Lidarr's body
  (`foreignArtistId`, `addOptions.monitor`, `monitorNewItems`), `PUT /api/v1/artist/{id}`
  edits like Lidarr.
- Monitor options when adding an artist: all, future, missing, existing, first, latest,
  none; "monitor new items" per artist.
- iCal feed at `/feed/v1/calendar/Chordarr.ics?apikey=…`.
- System → Backup (zip of database, API key and config; weekly, retention) and
  System → Updates (GitHub releases).

## 0.1.0 — 2026-11-01

First release.

- Web UI laid out like the *arr family: sidebar (Artists, Calendar, Activity, Wanted,
  Settings, System), poster grid with progress bars, artist page with sections per
  release type, agenda calendar, missing list, history, tasks. Default port 8585.

- Track artists by MusicBrainz id: albums, EPs, singles, plus features, remixes and
  compilation appearances with the exact track and role.
- Calendar, wanted list, activity timeline.
- Library matching by `MusicBrainz Release Group Id` tag, then by folder name.
- Import from Lidarr (database or API).
- Recommendations from ListenBrainz similar artists and Deezer related artists.
- Notifiers: generic JSON webhook, Discord, Matrix, ntfy.
- JSON API under `/api/v1`, OpenAPI docs at `/docs`.
- Native install script and systemd unit.
