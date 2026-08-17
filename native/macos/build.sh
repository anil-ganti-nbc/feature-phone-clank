#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$ROOT/native/macos/build"
exec "${PYTHON:-python3}" -m PyInstaller --noconfirm --clean --windowed --paths "$ROOT/src" --name "Feature Phone Clank" --onedir --specpath "$ROOT/native/macos/build" --distpath "$ROOT/native/macos/dist" --workpath "$ROOT/native/macos/build" --osx-bundle-identifier com.clank.featurephone.fieldtest --add-data "$ROOT/config:config" --add-data "$ROOT/src/feature_phone_clank/providers/sqlite/schema.sql:feature_phone_clank/providers/sqlite" "$ROOT/native/macos/launcher.py"
