#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-run}"
case "$MODE" in run|--build-only) ;; *) echo "Usage: $0 [--build-only]" >&2; exit 2 ;; esac
mkdir -p "$ROOT_DIR/build/cache"
xcrun swiftc -module-cache-path "$ROOT_DIR/build/cache/swift-cache" -O -framework Cocoa -framework WebKit "$ROOT_DIR/app/entry/macos/Converto.swift" -o "$ROOT_DIR/build/cache/shell-binary"
python3 "$ROOT_DIR/scripts/package.py"
APP="$ROOT_DIR/build/Dev App Dropper and Launcher.app"
if [[ "$MODE" == "--build-only" ]]; then exit 0; fi
"$APP/Contents/MacOS/Dev App Dropper and Launcher" --quit-running || true
open "$APP"
