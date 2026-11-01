#!/bin/sh
# Run as PUID:PGID (like the linuxserver.io and hotio images) so files created in
# /music and /downloads belong to the user who owns the library.
set -e
PUID="${PUID:-1000}"; PGID="${PGID:-1000}"
if [ "$(id -u)" = "0" ]; then
  groupmod -o -g "$PGID" chordarr >/dev/null 2>&1 || true
  usermod -o -u "$PUID" chordarr >/dev/null 2>&1 || true
  chown chordarr:chordarr /config
  exec gosu chordarr "$@"
fi
exec "$@"
