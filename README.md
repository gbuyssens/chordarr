# Chordarr

**Music collection manager for the \*arr stack.** Track artists the way they actually
release music in 2026: albums, EPs and singles, but also the **features**, remixes and
compilation appearances Lidarr never shows you. Get a release calendar, a wanted list,
recommendations built from what you already follow, and notifications anywhere
(webhook, Discord, Matrix, ntfy). Let Prowlarr push its indexers, grab missing releases
through qBittorrent or SABnzbd, import and file them, and have Jellyfin or Plex pick
them up.

Chordarr is a single Python service with a built-in web UI laid out like the other \*arr
applications (same sidebar, posters, calendar, wanted list, activity, settings) and a JSON API. No Docker
required, no Node build, no proprietary metadata proxy: it talks to MusicBrainz, the
Cover Art Archive, ListenBrainz and Deezer directly.

> Status: **0.2, early**. Catalogue, calendar, library matching, recommendations,
> notifications, Lidarr import, Prowlarr sync, search and grab, completed-download
> import with renaming, Jellyfin/Plex refresh all work. Automatic grabbing is off until
> you switch it on. See [Roadmap](#roadmap).

## Why not Lidarr?

| | Lidarr | Chordarr |
|---|---|---|
| Artist's own albums, EPs, singles | yes | yes |
| Features on other artists' tracks | no | **yes**, with the exact track and role |
| Compilation / soundtrack appearances | no | yes |
| Release calendar | yes | yes, including appearances |
| Recommendations | no | yes, ListenBrainz + Deezer, voted by your own artists |
| Metadata source | Lidarr's own metadata server (outages, stale cache) | MusicBrainz live, 1 req/s |
| Notifications | fixed list of providers | generic webhook + Discord, Matrix, ntfy |
| Indexers | Prowlarr sync, Torznab, Newznab | same: Prowlarr adds Chordarr as a "Lidarr" application, unchanged |
| Download clients | many | qBittorrent, SABnzbd (more on request) |
| Quality | profiles, custom formats | one profile: lossless, high or anything, best wins |
| Media servers | Plex, Emby/Jellyfin, Kodi | Jellyfin/Emby and Plex, targeted scan of the imported folder |
| Install | .NET runtime | Python ≥ 3.11, one systemd unit |
| Default port | 8686 | 8585 |

## Quick start

```bash
git clone https://github.com/gbuyssens/chordarr.git
cd chordarr
python3 -m venv .venv && .venv/bin/pip install .
CHORDARR_ROOT_FOLDER=/path/to/music .venv/bin/chordarr
```

Open <http://localhost:8585>. Add an artist, or import everything from Lidarr in
*Settings → Import from Lidarr* (point it at `lidarr.db` or at the Lidarr URL + API key).

For a server install (dedicated user, `/opt/chordarr`, `/var/lib/chordarr`, systemd):

```bash
sudo ./deploy/install.sh --group media   # the group that owns your music files
sudo editor /etc/chordarr/chordarr.env   # root folder, port
sudo systemctl restart chordarr
```

## Configuration

Environment variables prefixed `CHORDARR_`, or the same keys in `chordarr.toml` inside
the data directory. The API key is generated at first start and stored in
`<data dir>/api_key`.

| Variable | Default | Purpose |
|---|---|---|
| `CHORDARR_DATA_DIR` | `/var/lib/chordarr` (root) or `~/.config/chordarr` | database, API key, config file |
| `CHORDARR_PORT` / `CHORDARR_HOST` | `8585` / `0.0.0.0` | listen address |
| `CHORDARR_URL_BASE` | empty | sub-path when behind a reverse proxy (`/chordarr`) |
| `CHORDARR_ROOT_FOLDER` | unset | where artist folders live |
| `CHORDARR_ARTIST_FOLDER_FORMAT` | `{artist}` | artist folder name under the root |
| `CHORDARR_ALBUM_FOLDER_FORMAT` | `{album} ({year})` | release folder name under the artist |
| `CHORDARR_TRACK_FORMAT` | `{nn} - {title}` | imported file names; also `{track}`, `{disc}`, `{artist}`, `{album}`, `{year}` |
| `CHORDARR_IMPORT_MODE` | `auto` | `auto` hard-links torrents and moves usenet downloads; or `hardlink`, `copy`, `move` |
| `CHORDARR_AUTO_GRAB` | `false` | grab missing releases on your behalf (RSS and periodic search); the UI can override it |
| `CHORDARR_QUALITY_PROFILE` | `lossless` | minimum accepted quality: `lossless`, `high` (320/V0 too) or `any` |
| `CHORDARR_RSS_INTERVAL_MINUTES` / `CHORDARR_SEARCH_INTERVAL_HOURS` / `CHORDARR_QUEUE_POLL_SECONDS` | `30` / `6` / `60` | acquisition periods |
| `CHORDARR_UI_PASSWORD` | empty | enables a cookie login for the UI |
| `CHORDARR_AUTH_DISABLED_FOR_LOCAL` | `true` | accept API calls from private addresses without a key |
| `CHORDARR_REFRESH_INTERVAL_HOURS` | `12` | how often each artist is re-checked on MusicBrainz |
| `CHORDARR_SCAN_INTERVAL_HOURS` | `6` | library scan period |
| `CHORDARR_RECOMMENDATIONS_INTERVAL_HOURS` | `24` | recommendations rebuild period |
| `CHORDARR_MUSICBRAINZ_URL` | `https://musicbrainz.org/ws/2` | point at a mirror to lift the 1 req/s limit |
| `CHORDARR_LOG_LEVEL` | `info` | |

**Authentication.** The UI is open by default because the intended setup is a reverse
proxy with SSO in front (Authentik, Authelia, Cloudflare Access…), as for the other
\*arr apps. Set `CHORDARR_UI_PASSWORD` if you have none. The JSON API always accepts
the key in the `X-Api-Key` header or the `apikey` query parameter.

## How it works

* **Catalogue.** For each artist, Chordarr browses MusicBrainz for the release groups
  the artist is credited on (`release-group?artist=`) and for the releases where the
  artist is credited on *tracks* only (`release?track_artist=`). The second call is
  where features live; each one is stored with the recording title, the full credit
  ("Booba feat. Damso & Gato") and a role: *featured*, *remixer*, *compilation*,
  *credited*.
* **Library.** Releases are matched to folders under the artist folder, first by the
  `MusicBrainz Release Group Id` tag found in the audio files (Picard, beets and Lidarr
  all write it), then by normalised folder name with or without a trailing `(year)`.
  Nothing on disk is ever renamed or moved in 0.1.
* **Calendar and wanted list.** Monitored releases with a date are the calendar; those
  already out and not on disk are the wanted list. By default an artist's studio albums,
  EPs and singles are monitored, plus the releases it is featured on; live albums,
  compilations, remixes and compilation-only appearances are listed but not wanted until
  you switch them on.
* **Recommendations.** Each tracked artist votes for its ListenBrainz "similar artists"
  (listening-session based, open data) and Deezer "related artists". Candidates several
  of your artists point to rise to the top. One click tracks them.
* **Indexers.** Add Chordarr to Prowlarr as a **Lidarr** application (Chordarr URL and
  API key): Prowlarr tests it, pushes every indexer that supports music categories and
  keeps them in sync, through the same `/api/v1/indexer` resources Lidarr exposes.
  Torznab and Newznab indexers can also be added by hand in *Settings → Indexers*.
* **Search and grab.** A result must name the artist and the release, meet the quality
  profile (`lossless` > `high` > anything) and the indexer's minimum seeders; among the
  accepted ones the best quality wins, then seeders, then indexer priority. Every missing
  release has a *Search* page where you pick a result yourself. With automatic grabbing on
  (*Settings → Indexers → Options*), RSS feeds are read every 30 minutes and missing
  releases are searched every 6 hours, and the best candidate goes to the download client.
* **Download clients and import.** qBittorrent (category `chordarr`) and SABnzbd. The
  queue is polled every minute; when a download finishes its tags are read (mutagen) and
  checked against the expected release (release-group id, then album title and artist),
  tracks are filed as `{root}/{artist}/{album} ({year})/{nn} - {title}.ext`, the cover is
  copied, the release becomes *have* and `release_imported` fires. Torrents are
  hard-linked so they keep seeding (copied across file systems), usenet downloads are
  moved. A folder already on disk can be imported from *Activity → Queue*. Remote path
  mappings cover clients on another machine.
* **Media servers.** After each import Jellyfin/Emby gets `POST /Library/Media/Updated`
  and Plex a section refresh for the imported folder only, with a local → remote path
  mapping when they see the library under another path.
* **Notifications.** Events (release found, upcoming release, appearance found, release
  grabbed, release imported, download or import failed, errors…) are dispatched to every
  enabled notifier. The webhook payload is
  plain JSON, built for n8n, Home Assistant or your own scripts:

  ```json
  {"source": "chordarr", "event": "appearance_found",
   "title": "Damso appears on Booba – Nero Nemesis",
   "body": "Track: Pinocchio (Booba feat. Damso & Gato)",
   "artist": {"id": 1, "mbid": "71ff695e-…", "name": "Damso"},
   "release": {"mbid": "…", "title": "Nero Nemesis", "type": "Album", "kind": "appearance", "date": "2015-12-04"},
   "recording": {"mbid": "…", "title": "Pinocchio", "role": "featured"},
   "created_at": "2026-11-01T18:04:11"}
  ```

## API

Everything the UI does is available under `/api/v1` (OpenAPI docs at `/docs`).

| Endpoint | |
|---|---|
| `GET /system/status`, `GET /health` | instance info, scheduler state, problems |
| `GET /artist/lookup?term=` | MusicBrainz search |
| `GET/POST /artist`, `GET/PATCH/DELETE /artist/{id}`, `POST /artist/{id}/refresh` | tracked artists (`POST` takes `{"mbid": …}`) |
| `GET /artist/{id}/release`, `GET /artist/{id}/appearance` | catalogue of one artist |
| `GET /release`, `PATCH /release/{id}`, `GET /calendar`, `GET /wanted` | releases |
| `GET /recommendation`, `POST /recommendation/{id}/add|dismiss` | discovery |
| `GET /event`, `GET/POST/PUT/DELETE /notification`, `POST /notification/{id}/test` | timeline and notifiers |
| `GET /indexer/schema`, `GET/POST/PUT/DELETE /indexer`, `POST /indexer/test`, `GET/POST/DELETE /tag` | Lidarr-shaped, what Prowlarr drives |
| `GET /downloadclient/schema`, `GET/POST/PUT/DELETE /downloadclient`, `POST /downloadclient/{id}/test` | Lidarr-shaped (`QBittorrent`, `Sabnzbd`) |
| `GET /queue` (paged, Lidarr record names), `DELETE /queue/{id}`, `POST /queue/{id}/import` | the download queue |
| `GET /release/{id}/search`, `POST /release/{id}/grab` | interactive search, `{"guid": …, "indexer_id": …}` |
| `POST /import/manual` | `{"path": …, "release_id": optional, "force": false}` |
| `GET/POST/PUT/DELETE /mediaserver`, `POST /mediaserver/{id}/test`, `POST /mediaserver/{id}/scan` | Jellyfin and Plex |
| `GET /qualityprofile`, `GET/PUT /config/acquisition` | the three profiles; `auto_grab` and `quality_profile` |
| `POST /import/lidarr` | `{"db_path": …}` or `{"url": …, "api_key": …}` |
| `POST /command/{refresh,scan,recommendations,notify,rss,search,queue}` | run a job now |

`system/status`, the artist endpoints and everything acquisition-related mirror Lidarr's
paths and field names (`foreignArtistId`, `fields: [{name, value}]`, `implementation`,
`configContract`), so Prowlarr and existing tooling have little to change. API answers
carry `X-Application-Version` (the Lidarr API level Chordarr speaks, 1.0.2) and
`X-Chordarr-Version`.

### Replacing Lidarr

1. Install Chordarr next to Lidarr and import your artists (*Artists → Library Import*).
2. In Prowlarr, add an application of type **Lidarr** pointing at Chordarr with its API
   key; the indexers appear in *Settings → Indexers*.
3. Add your download client in *Settings → Download Clients* with its own category
   (`chordarr`), and Jellyfin or Plex in *Settings → Connect*.
4. Open a missing release, *Search*, grab one result and watch *Activity → Queue* import
   it. When you are happy, turn on automatic grabbing in *Settings → Indexers → Options*
   and switch Lidarr off.

## Roadmap

1. **0.1 — catalogue** (this release): artists, releases, features and appearances,
   calendar, wanted list, library matching, Lidarr import, recommendations, notifiers.
2. **0.2 — acquisition** (this release): Prowlarr sync through the Lidarr-compatible
   indexer API, Torznab and Newznab search and RSS, qBittorrent and SABnzbd,
   completed-download import with renaming, manual folder import, Jellyfin and Plex
   targeted refresh.
3. **0.3 — Lidarr compatibility layer**: the rest of Lidarr's v1 API surface
   (`/album`, `/track`, `/history`, `/remotepathmapping`, media servers as
   `/notification` connections…) so third-party tools see a Lidarr; Transmission and
   Deluge; several root folders.
4. **0.4 — listening**: Jellyfin and Plex play history as a recommendation signal,
   Last.fm and ListenBrainz scrobbles, "what's missing from artists I actually play".
5. Later: real quality profiles and custom formats, Lucida-style direct sources as an
   optional plugin, i18n.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/ruff check chordarr tests
CHORDARR_DATA_DIR=./data .venv/bin/chordarr --reload
```

Please be gentle with MusicBrainz: the client already enforces one request per second
and identifies itself; keep it that way, or point `CHORDARR_MUSICBRAINZ_URL` at your
own mirror.

## Credits

Metadata from [MusicBrainz](https://musicbrainz.org) and the
[Cover Art Archive](https://coverartarchive.org), similarity data from
[ListenBrainz](https://listenbrainz.org), artist pictures and related artists from
Deezer's public API. Not affiliated with the Servarr team; the name is a nod to the
family it is meant to live in.

MIT license.
