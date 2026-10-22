#!/usr/bin/env bash
set -euo pipefail
set +x
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PROJECT_ID=${PROJECT_ID:-winged-ratio-377512}
export REGION=${REGION:-europe-north1}
export SCHEDULER_LOCATION=${SCHEDULER_LOCATION:-europe-west1}
export EVENTS_CHANNEL_ID=${EVENTS_CHANNEL_ID:-@eventhsl}
SERVICE=finland-news-bot
python3 scripts/diagnose.py --preflight
python3 -m compileall -q app
PREVIOUS_REVISION=$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --format='value(status.latestReadyRevisionName)')
RUNTIME=$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --format='value(spec.template.spec.serviceAccountName)')
APP_VERSION=$(git rev-parse --short HEAD)
printf 'Previous revision: %s\n' "$PREVIOUS_REVISION"
# Allows /collect to trigger the two existing Scheduler jobs without a long Telegram request.
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$RUNTIME" \
  --role=roles/cloudscheduler.jobRunner --quiet >/dev/null
gcloud run deploy "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --source . \
  --timeout=300 --update-env-vars="EVENTS_CHANNEL_ID=$EVENTS_CHANNEL_ID,ADMIN_AUTH_MODE=channel_admins,SCHEDULER_LOCATION=$SCHEDULER_LOCATION,APP_VERSION=$APP_VERSION"
# Preserve existing channel, service account, secrets, AI models, publication settings and traffic semantics.
python3 scripts/diagnose.py --preflight --expected-version "$APP_VERSION"
python3 scripts/diagnose.py --migrate
unset SERVICE_URL CRON_SECRET
bash scripts/configure-scheduler.sh
python3 scripts/diagnose.py --preflight --expected-version "$APP_VERSION"
printf '\nDeployed %s. Open /start in @suominews_bot.\n' "$APP_VERSION"
printf 'Rollback code if needed: gcloud run services update-traffic %s --project=%s --region=%s --to-revisions=%s=100\n' "$SERVICE" "$PROJECT_ID" "$REGION" "$PREVIOUS_REVISION"
printf 'Events auto-publication starts OFF; choose Events → Settings after checking the first drafts.\n'
