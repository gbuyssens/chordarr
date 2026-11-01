#!/usr/bin/env bash
# Cut a release: bump the version in pyproject.toml and chordarr/__init__.py, check that
# CHANGELOG.md has a section for it, commit, tag v<version>, push. CI does the rest.
#   scripts/release.sh 0.2.0
set -euo pipefail
v="${1:?version, e.g. 0.2.0}"
cd "$(dirname "$0")/.."
[[ -z "$(git status --porcelain)" ]] || { echo "working tree not clean" >&2; exit 1; }
grep -q "^## $v " CHANGELOG.md || { echo "CHANGELOG.md has no '## $v — date' section" >&2; exit 1; }
sed -i "s/^version = \".*\"/version = \"$v\"/" pyproject.toml
sed -i "s/^__version__ = \".*\"/__version__ = \"$v\"/" chordarr/__init__.py
git add pyproject.toml chordarr/__init__.py
git commit -m "Release $v"
git tag -a "v$v" -m "Chordarr $v"
git push && git push origin "v$v"
echo "Tagged v$v. Watch https://github.com/gbuyssens/chordarr/actions"
