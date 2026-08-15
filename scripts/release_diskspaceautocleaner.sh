#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-3.9.23}"
PLUGIN_DIR="plugins.v2/diskspaceautocleaner"
ZIP_NAME="diskspaceautocleaner_v${VERSION}.zip"
TMPDIR="/tmp/diskspaceautocleaner-release"

rm -rf "$TMPDIR" "$ZIP_NAME"
mkdir -p "$TMPDIR/diskspaceautocleaner"
cp -a "$PLUGIN_DIR/." "$TMPDIR/diskspaceautocleaner/"
(cd "$TMPDIR" && zip -r "$OLDPWD/$ZIP_NAME" diskspaceautocleaner >/dev/null)
echo "$ZIP_NAME"
