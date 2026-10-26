#!/usr/bin/env bash
set -euo pipefail
set +x
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TARGET=${1:-"$HOME/projects/bot"}
if [ ! -f "$TARGET/app/main.py" ]; then
  echo "Existing bot source missing: $TARGET/app/main.py" >&2
  exit 1
fi
bash "$ROOT/releases/2026-10-26/recovery/update.sh" "$TARGET"
