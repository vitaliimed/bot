#!/usr/bin/env bash
set -euo pipefail
set +x
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PROJECT_ID=${PROJECT_ID:-winged-ratio-377512}
export REGION=${REGION:-europe-north1}
export SCHEDULER_LOCATION=${SCHEDULER_LOCATION:-europe-west1}
python3 -m compileall -q app
# Preserve the service account, access mode, channel and every existing secret.
gcloud run services describe finland-news-bot --project="$PROJECT_ID" \
  --region="$REGION" --format='value(metadata.name)' >/dev/null
gcloud run deploy finland-news-bot --project="$PROJECT_ID" --region="$REGION" \
  --source . --timeout=300
unset SERVICE_URL CRON_SECRET
bash scripts/configure-scheduler.sh
SERVICE_URL=$(gcloud run services describe finland-news-bot --project="$PROJECT_ID" \
  --region="$REGION" --format='value(status.url)')
curl -fsS "$SERVICE_URL/health"
printf '\nEditor upgrade deployed. Open /settings in Telegram.\n'
