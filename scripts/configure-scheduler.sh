#!/usr/bin/env bash
set -euo pipefail
set +x

: "${PROJECT_ID:?Set PROJECT_ID}"
REGION=${REGION:-europe-north1}
SCHEDULER_LOCATION=${SCHEDULER_LOCATION:-europe-west1}
SERVICE=${SERVICE:-finland-news-bot}
JOB=finland-news-collect

if [[ -z "${SERVICE_URL:-}" ]]; then
  SERVICE_URL=$(gcloud run services describe "$SERVICE" \
    --project="$PROJECT_ID" --region="$REGION" --format='value(status.url)')
fi
[[ "$SERVICE_URL" == https://* ]] || { echo 'Cloud Run service URL is missing'; exit 1; }

trap 'unset CRON_SECRET' EXIT
if [[ -z "${CRON_SECRET:-}" ]]; then
  CRON_SECRET=$(gcloud secrets versions access latest \
    --project="$PROJECT_ID" --secret=cron-secret)
fi
: "${CRON_SECRET:?Stored cron-secret is empty}"

common_args=(
  --project="$PROJECT_ID"
  --location="$SCHEDULER_LOCATION"
  --schedule='*/30 * * * *'
  --time-zone=Europe/Helsinki
  --uri="${SERVICE_URL%/}/collect"
  --http-method=POST
  --attempt-deadline=300s
  --format='value(name)'
)

# Scheduler stores the header secret in its job configuration; restrict job IAM access.
if gcloud scheduler jobs describe "$JOB" --project="$PROJECT_ID" \
    --location="$SCHEDULER_LOCATION" --format='value(name)' >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$JOB" "${common_args[@]}" \
    --update-headers="X-Cron-Secret=$CRON_SECRET"
else
  gcloud scheduler jobs create http "$JOB" "${common_args[@]}" \
    --headers="X-Cron-Secret=$CRON_SECRET"
fi

gcloud scheduler jobs describe "$JOB" --project="$PROJECT_ID" \
  --location="$SCHEDULER_LOCATION" \
  --format='yaml(name,state,schedule,timeZone,httpTarget.uri,lastAttemptTime,status)'
