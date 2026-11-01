#!/usr/bin/env bash
# Native install on Debian/Ubuntu (systemd). Idempotent: re-run to upgrade.
#   sudo ./deploy/install.sh [--user chordarr] [--group media] [--prefix /opt/chordarr]
set -euo pipefail

USER_NAME=chordarr; GROUP_NAME=""; PREFIX=/opt/chordarr; DATA_DIR=/var/lib/chordarr
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) USER_NAME="$2"; shift 2;;
    --group) GROUP_NAME="$2"; shift 2;;
    --prefix) PREFIX="$2"; shift 2;;
    --data) DATA_DIR="$2"; shift 2;;
    *) echo "unknown option $1" >&2; exit 2;;
  esac
done
GROUP_NAME="${GROUP_NAME:-$USER_NAME}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "python >= 3.11 is required" >&2; exit 1; }
python3 -m venv --help >/dev/null 2>&1 || apt-get install -y python3-venv

getent group "$GROUP_NAME" >/dev/null || groupadd --system "$GROUP_NAME"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --gid "$GROUP_NAME" --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$USER_NAME"

mkdir -p "$PREFIX" "$DATA_DIR" /etc/chordarr
if [[ "$SRC" != "$PREFIX" ]]; then
  rsync -a --delete --exclude .venv --exclude .git --exclude '__pycache__' --exclude data "$SRC"/ "$PREFIX"/
fi
[[ -d "$PREFIX/.venv" ]] || python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/.venv/bin/pip" install --quiet "$PREFIX"
chown -R "$USER_NAME:$GROUP_NAME" "$DATA_DIR"
chown -R root:"$GROUP_NAME" "$PREFIX"

[[ -f /etc/chordarr/chordarr.env ]] || { sed "s#/var/lib/chordarr#$DATA_DIR#" "$SRC/deploy/chordarr.env.example" > /etc/chordarr/chordarr.env; chmod 640 /etc/chordarr/chordarr.env; chgrp "$GROUP_NAME" /etc/chordarr/chordarr.env; }
sed -e "s#^User=.*#User=$USER_NAME#" -e "s#^Group=.*#Group=$GROUP_NAME#" -e "s#/opt/chordarr#$PREFIX#g" "$SRC/deploy/chordarr.service" > /etc/systemd/system/chordarr.service
systemctl daemon-reload
systemctl enable --now chordarr
systemctl restart chordarr
sleep 2
systemctl --no-pager --lines=5 status chordarr || true
echo
echo "Chordarr installed. Edit /etc/chordarr/chordarr.env (root folder, port) then: systemctl restart chordarr"
echo "API key: $(cat "$DATA_DIR/api_key" 2>/dev/null || echo '(generated at first start)')"
