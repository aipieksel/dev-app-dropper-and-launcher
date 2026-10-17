#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT_DIR/app"
export CONVERTO_PORT="${CONVERTO_PORT:-8777}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
exec python3 -m entry
