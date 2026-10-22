#!/usr/bin/env bash
set -euo pipefail
set +x
: "${PROJECT_ID:?Set PROJECT_ID}"
REGION=${REGION:-europe-north1}
SCHEDULER_LOCATION=${SCHEDULER_LOCATION:-europe-west1}
SERVICE=${SERVICE:-finland-news-bot}
COLLECT_SCHEDULE=${COLLECT_SCHEDULE:-'*/15 * * * *'}
EVENTS_SCHEDULE=${EVENTS_SCHEDULE:-'7,22,37,52 * * * *'}
if [[ -z "${SERVICE_URL:-}" ]]; then
  SERVICE_URL=$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --format='value(status.url)')
fi
[[ "$SERVICE_URL" == https://* ]] || { echo 'Service URL missing'; exit 1; }
trap 'unset CRON_SECRET' EXIT
if [[ -z "${CRON_SECRET:-}" ]]; then
  CRON_SECRET=$(gcloud secrets versions access latest --project="$PROJECT_ID" --secret=cron-secret)
fi
for route in news events; do
  if [[ "$route" == news ]]; then
    JOB=finland-news-collect; URI="${SERVICE_URL%/}/collect"; SCHEDULE=$COLLECT_SCHEDULE
  else
    JOB=finland-events-collect; URI="${SERVICE_URL%/}/collect/events"; SCHEDULE=$EVENTS_SCHEDULE
  fi
  common=(--project="$PROJECT_ID" --location="$SCHEDULER_LOCATION" --schedule="$SCHEDULE"
    --time-zone=Europe/Helsinki --uri="$URI" --http-method=POST --attempt-deadline=300s --format='value(name)')
  if gcloud scheduler jobs describe "$JOB" --project="$PROJECT_ID" --location="$SCHEDULER_LOCATION" --format='value(name)' >/dev/null 2>&1; then
    gcloud scheduler jobs update http "$JOB" "${common[@]}" --update-headers="X-Cron-Secret=$CRON_SECRET"
  else
    gcloud scheduler jobs create http "$JOB" "${common[@]}" --headers="X-Cron-Secret=$CRON_SECRET"
  fi
  gcloud scheduler jobs describe "$JOB" --project="$PROJECT_ID" --location="$SCHEDULER_LOCATION" \
    --format='yaml(name,state,schedule,timeZone,httpTarget.uri,lastAttemptTime,status)'
done
