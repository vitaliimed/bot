#!/usr/bin/env bash
set -euo pipefail
set +x

cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PROJECT_ID="${PROJECT_ID:-winged-ratio-377512}"
export REGION="${REGION:-europe-north1}"
export SCHEDULER_LOCATION="${SCHEDULER_LOCATION:-europe-west1}"
CHANNEL_ID=${CHANNEL_ID:-@suominewsua}
SERVICE=finland-news-bot

# Require an existing service so all of its other runtime settings are retained.
gcloud run services describe "$SERVICE" --project="$PROJECT_ID" \
  --region="$REGION" --format='value(metadata.name)' >/dev/null

gcloud run deploy "$SERVICE" --project="$PROJECT_ID" --region="$REGION" \
  --source . \
  --update-env-vars="ADMIN_AUTH_MODE=channel_admins,ADMIN_ID=0,CHANNEL_ID=$CHANNEL_ID"

# Read the current stored secret when finishing the existing Scheduler setup.
unset CRON_SECRET SERVICE_URL
bash scripts/configure-scheduler.sh

SERVICE_URL=$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" \
  --region="$REGION" --format='value(status.url)')
curl -fsS "$SERVICE_URL/health"
printf '\nChannel administrator access enabled. Open your bot and send /start.\n'
