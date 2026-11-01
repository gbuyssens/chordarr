# Changelog

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
