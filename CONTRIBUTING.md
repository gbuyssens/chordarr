# Contributing

Chordarr follows the conventions of the *arr family wherever they make sense, so that
people who run Lidarr, Radarr or Sonarr feel at home.

## Layout

| Path | What |
|---|---|
| `chordarr/` | the application: `config.py`, `db.py`, `models.py`, `app.py` |
| `chordarr/metadata/` | upstream clients (MusicBrainz, Deezer, ListenBrainz, Cover Art Archive) |
| `chordarr/services/` | the logic: artists, library, import, recommendations, notify, scheduler |
| `chordarr/api/` | `router.py` (JSON API `/api/v1`), `ui.py` (pages), `auth.py` |
| `chordarr/templates/`, `chordarr/static/` | server-rendered UI, no build step |
| `deploy/` | native install: `install.sh`, systemd unit, env example |
| `docker/`, `Dockerfile`, `docker-compose.yml` | the image, `/config` `/music` `/downloads`, `PUID`/`PGID` |
| `scripts/` | maintainer scripts (`release.sh`) |
| `tests/` | pytest, network stubbed |

Data directory (`/config` in Docker, `/var/lib/chordarr` natively): `chordarr.db`,
`api_key`, optional `chordarr.toml`.

## Branches and releases

* `main` is always releasable; features land through pull requests with CI green
  (`ruff`, `pytest` on Python 3.11 to 3.13, Docker build).
* Versions are semantic. `scripts/release.sh 0.2.0` bumps, tags `v0.2.0` and pushes;
  the release workflow runs the tests, publishes `ghcr.io/gbuyssens/chordarr`
  (`0.2.0`, `0.2`, `latest`, amd64 + arm64) and creates the GitHub release from the
  matching `CHANGELOG.md` section.
* Every user-visible change gets a line in `CHANGELOG.md` under the next version.

## Code

* Python ≥ 3.11, type hints, `ruff` clean (`line-length 110`).
* Network first, database second: fetch everything, then write in one short
  transaction (SQLite holds a single write lock).
* MusicBrainz is shared infrastructure: one request per second through the common
  limiter, a real User-Agent, no scraping.
* Field names on the API mirror Lidarr's where there is no reason to differ
  (`foreignArtistId`, `artistName`, `monitored`, `qualityProfileId`…), so existing
  tooling needs little change.
* English in code, comments, UI strings and docs.
