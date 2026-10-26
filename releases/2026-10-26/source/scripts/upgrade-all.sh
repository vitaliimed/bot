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
python3 scripts/backup-state.py
python3 -m compileall -q app
PREVIOUS_REVISION=$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --format='value(status.latestReadyRevisionName)')
PREVIOUS_TRAFFIC=$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --format=json | python3 -c 'import json,sys; print(",".join(x["revisionName"]+"="+str(x["percent"]) for x in json.load(sys.stdin)["status"]["traffic"] if x.get("percent",0)))')
RUNTIME=$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --format='value(spec.template.spec.serviceAccountName)')
APP_VERSION=$(python3 scripts/source-version.py)
printf 'Previous revision: %s\n' "$PREVIOUS_REVISION"
# Allows /collect to trigger the two existing Scheduler jobs without a long Telegram request.
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$RUNTIME" \
  --role=roles/cloudscheduler.jobRunner --quiet >/dev/null
RUNTIME="$RUNTIME" bash scripts/configure-ai-keys.sh
gcloud run deploy "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --source . \
  --no-traffic --tag=bot-candidate --timeout=300 --update-env-vars="EVENTS_CHANNEL_ID=$EVENTS_CHANNEL_ID,ADMIN_AUTH_MODE=channel_admins,SCHEDULER_LOCATION=$SCHEDULER_LOCATION,APP_VERSION=$APP_VERSION,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,AI_KEY_MANAGEMENT=1"
CANDIDATE_URL=$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --format=json | python3 -c 'import json,sys; print(next(x["url"] for x in json.load(sys.stdin)["status"]["traffic"] if x.get("tag")=="bot-candidate"))')
NEW_REVISION=$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --format='value(status.latestReadyRevisionName)')
python3 scripts/diagnose.py --candidate "$CANDIDATE_URL" --expected-version "$APP_VERSION"
printf 'Candidate verified. Previous traffic: %s\n' "$PREVIOUS_TRAFFIC"
gcloud run services update-traffic "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --to-revisions="$NEW_REVISION=100"
# Preserve existing channel, service account, secrets, AI models, publication settings and traffic semantics.
python3 scripts/diagnose.py --preflight --expected-version "$APP_VERSION"
python3 scripts/diagnose.py --migrate
unset SERVICE_URL CRON_SECRET
bash scripts/configure-scheduler.sh
bash scripts/configure-watchdog.sh
bash scripts/configure-dispatch.sh
python3 scripts/diagnose.py --preflight --expected-version "$APP_VERSION"
printf '\nDeployed %s. Open /start in @suominews_bot.\n' "$APP_VERSION"
printf 'Persistent menu: /menu. AI: /ai. All drafts / destinations: /unpublished. Why not posting: /diagnose. Import: /import.\n'
printf 'Rollback code if needed: gcloud run services update-traffic %s --project=%s --region=%s --to-revisions=%s\n' "$SERVICE" "$PROJECT_ID" "$REGION" "$PREVIOUS_TRAFFIC"
printf 'Existing auto-publication settings are preserved. New event channels start with auto OFF.\n'
