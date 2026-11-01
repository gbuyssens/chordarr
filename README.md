# Chordarr

**Music collection manager for the \*arr stack.** Track artists the way they actually
release music in 2026: albums, EPs and singles, but also the **features**, remixes and
compilation appearances Lidarr never shows you. Get a release calendar, a wanted list,
recommendations built from what you already follow, and notifications anywhere
(webhook, Discord, Matrix, ntfy).

Chordarr is a single Python service with a built-in web UI and a JSON API. No Docker
required, no Node build, no proprietary metadata proxy: it talks to MusicBrainz, the
Cover Art Archive, ListenBrainz and Deezer directly.

> Status: **0.1, early**. Catalogue, calendar, library matching, recommendations,
> notifications and Lidarr import work. Acquisition (Prowlarr, download clients, import
> and renaming) and media-server refresh (Jellyfin, Plex) are the next milestones; see
> [Roadmap](#roadmap).

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
| Install | .NET runtime | Python ≥ 3.11, one systemd unit |

## Quick start

```bash
git clone https://github.com/gbuyssens/chordarr.git
cd chordarr
python3 -m venv .venv && .venv/bin/pip install .
CHORDARR_ROOT_FOLDER=/path/to/music .venv/bin/chordarr
```

Open <http://localhost:8688>. Add an artist, or import everything from Lidarr in
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
| `CHORDARR_PORT` / `CHORDARR_HOST` | `8688` / `0.0.0.0` | listen address |
| `CHORDARR_URL_BASE` | empty | sub-path when behind a reverse proxy (`/chordarr`) |
| `CHORDARR_ROOT_FOLDER` | unset | where artist folders live |
| `CHORDARR_ARTIST_FOLDER_FORMAT` | `{artist}` | artist folder name under the root |
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
  already out and not on disk are the wanted list.
* **Recommendations.** Each tracked artist votes for its ListenBrainz "similar artists"
  (listening-session based, open data) and Deezer "related artists". Candidates several
  of your artists point to rise to the top. One click tracks them.
* **Notifications.** Events (release found, upcoming release, appearance found, release
  imported, errors…) are dispatched to every enabled notifier. The webhook payload is
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
| `POST /import/lidarr` | `{"db_path": …}` or `{"url": …, "api_key": …}` |
| `POST /command/{refresh,scan,recommendations,notify}` | run a job now |

`system/status` and the artist endpoints mirror Lidarr's field names where it is free
to do so (`foreignArtistId`, `artistName`), so existing tooling has little to change.

## Roadmap

1. **0.1 — catalogue** (this release): artists, releases, features and appearances,
   calendar, wanted list, library matching, Lidarr import, recommendations, notifiers.
2. **0.2 — acquisition**: Prowlarr sync (Lidarr-compatible indexer API), Torznab and
   Newznab search, qBittorrent, Transmission and SABnzbd, completed-download import with
   renaming, Jellyfin and Plex library refresh.
3. **0.3 — listening**: Jellyfin and Plex play history as a recommendation signal,
   Last.fm and ListenBrainz scrobbles, "what's missing from artists I actually play".
4. Later: multiple root folders and quality profiles, Lucida-style direct sources as an
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
