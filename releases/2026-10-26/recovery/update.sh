#!/usr/bin/env bash
set -euo pipefail
set +x
PATCH_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TARGET_DIR=${1:-"$HOME/projects/bot"}
cd "$TARGET_DIR"
PROJECT_ID=${PROJECT_ID:-winged-ratio-377512}
REGION=${REGION:-europe-north1}
SERVICE=finland-news-bot
python3 "$PATCH_DIR/apply.py" "$TARGET_DIR"
python3 -m compileall -q app
if [ -f scripts/backup-state.py ]; then PROJECT_ID="$PROJECT_ID" REGION="$REGION" python3 scripts/backup-state.py; fi
TRAFFIC=$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --format=json | python3 -c 'import sys,json; print(",".join(v["revisionName"]+"="+str(v["percent"]) for v in json.load(sys.stdin)["status"]["traffic"] if v.get("percent",0)))')
VERSION="recovery-$(date -u +%Y%m%d-%H%M%S)"
gcloud run deploy "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --source . --no-traffic --tag=recovery-candidate --timeout=300 --update-env-vars="APP_VERSION=$VERSION"
CANDIDATE=$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --format=json | python3 -c 'import sys,json; print(next(v["url"] for v in json.load(sys.stdin)["status"]["traffic"] if v.get("tag")=="recovery-candidate"))')
python3 "$PATCH_DIR/diagnose.py" --url "$CANDIDATE" --project "$PROJECT_ID" --expected "$VERSION"
REVISION=$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --format='value(status.latestReadyRevisionName)')
gcloud run services update-traffic "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --to-revisions="$REVISION=100"
python3 "$PATCH_DIR/diagnose.py" --project "$PROJECT_ID" --region "$REGION"
printf '\nRollback: gcloud run services update-traffic %s --project=%s --region=%s --to-revisions=%s\n' "$SERVICE" "$PROJECT_ID" "$REGION" "$TRAFFIC"
printf 'Open /repair in Telegram. Existing Scheduler schedules have not been changed.\n'
