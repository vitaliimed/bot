#!/usr/bin/env bash
set -euo pipefail
set +x
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TARGET=${1:-"$HOME/projects/bot"}
export PROJECT_ID=${PROJECT_ID:-winged-ratio-377512}
export REGION=${REGION:-europe-north1}
if [ ! -f "$TARGET/app/main.py" ]; then
  echo "Existing bot source missing: $TARGET/app/main.py" >&2
  exit 1
fi
if ! python3 "$ROOT/scripts/recover-live-source.py" --check "$TARGET"; then
  echo 'Local source is an older version. Recovering the source of the live Cloud Run image.'
  echo 'The current project folder will remain unchanged.'
  RESULT_FILE=$(mktemp)
  trap 'rm -f "$RESULT_FILE"' EXIT
  python3 "$ROOT/scripts/recover-live-source.py" --project "$PROJECT_ID" --region "$REGION" --output-file "$RESULT_FILE"
  TARGET=$(cat "$RESULT_FILE")
fi
bash "$ROOT/releases/2026-10-26/recovery/update.sh" "$TARGET"
printf '\nSource used for this deployment: %s\n' "$TARGET"
