#!/usr/bin/env bash
set -euo pipefail
set +x
PROJECT_ID=${PROJECT_ID:-winged-ratio-377512}
REGION=${REGION:-europe-north1}
SCHEDULER_LOCATION=${SCHEDULER_LOCATION:-europe-west1}
SERVICE_URL=$(gcloud run services describe finland-news-bot --project="$PROJECT_ID" --region="$REGION" --format='value(status.url)')
CRON_SECRET=$(gcloud secrets versions access latest --project="$PROJECT_ID" --secret=cron-secret)
args=(--project="$PROJECT_ID" --location="$SCHEDULER_LOCATION" --schedule='*/30 * * * *' --time-zone=Europe/Helsinki --uri="$SERVICE_URL/admin/watchdog" --http-method=POST --attempt-deadline=120s)
if gcloud scheduler jobs describe finland-news-watchdog --project="$PROJECT_ID" --location="$SCHEDULER_LOCATION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http finland-news-watchdog "${args[@]}" --update-headers="X-Cron-Secret=$CRON_SECRET" >/dev/null
else
  gcloud scheduler jobs create http finland-news-watchdog "${args[@]}" --headers="X-Cron-Secret=$CRON_SECRET" >/dev/null
fi
unset CRON_SECRET
printf 'Independent queue/webhook watchdog configured. No news publication is performed by this job.\n'
