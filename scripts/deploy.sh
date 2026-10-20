#!/usr/bin/env bash
set -euo pipefail
: "${PROJECT_ID:?set PROJECT_ID}"
: "${ADMIN_ID:=0}"
: "${ADMIN_AUTH_MODE:=id}"
: "${CHANNEL_ID:?set CHANNEL_ID}"
: "${BOT_TOKEN:?set BOT_TOKEN}"
: "${WEBHOOK_SECRET:?set WEBHOOK_SECRET}"
: "${CRON_SECRET:?set CRON_SECRET}"
REGION=${REGION:-europe-north1}
SCHEDULER_LOCATION=${SCHEDULER_LOCATION:-europe-west1}
GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.5-flash-lite}
SERVICE=finland-news-bot
SA=finland-news-runtime
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com firestore.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com
gcloud iam service-accounts create "$SA" --display-name='Finland News runtime' 2>/dev/null || true
RUNTIME="$SA@$PROJECT_ID.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$RUNTIME" --role='roles/datastore.user' --quiet >/dev/null
for name in bot-token webhook-secret cron-secret gemini-api-key; do
  gcloud secrets describe "$name" >/dev/null 2>&1 || gcloud secrets create "$name" --replication-policy=automatic
  gcloud secrets add-iam-policy-binding "$name" --member="serviceAccount:$RUNTIME" --role='roles/secretmanager.secretAccessor' --quiet >/dev/null
done
printf %s "$BOT_TOKEN" | gcloud secrets versions add bot-token --data-file=- >/dev/null
printf %s "$WEBHOOK_SECRET" | gcloud secrets versions add webhook-secret --data-file=- >/dev/null
printf %s "$CRON_SECRET" | gcloud secrets versions add cron-secret --data-file=- >/dev/null
printf %s "${GEMINI_API_KEY:-}" | gcloud secrets versions add gemini-api-key --data-file=- >/dev/null
# Firestore Native database must exist. Create once in console or run firestore databases create.
if ! gcloud firestore databases describe --database='(default)' >/dev/null 2>&1; then
  gcloud firestore databases create --database='(default)' --location="$REGION" --type=firestore-native
fi
gcloud run deploy "$SERVICE" --source . --region "$REGION" --service-account="$RUNTIME" --allow-unauthenticated --min-instances=0 --max-instances=2 --concurrency=8 --memory=512Mi --timeout=300 --set-env-vars="ADMIN_ID=$ADMIN_ID,ADMIN_AUTH_MODE=$ADMIN_AUTH_MODE,CHANNEL_ID=$CHANNEL_ID,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GEMINI_MODEL=$GEMINI_MODEL" --set-secrets='BOT_TOKEN=bot-token:latest,WEBHOOK_SECRET=webhook-secret:latest,CRON_SECRET=cron-secret:latest,GEMINI_API_KEY=gemini-api-key:latest'
URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
curl -fsS "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" --data-urlencode "url=$URL/telegram" --data-urlencode "secret_token=$WEBHOOK_SECRET" --data-urlencode 'allowed_updates=["message","callback_query"]'
echo
PROJECT_ID="$PROJECT_ID" REGION="$REGION" SCHEDULER_LOCATION="$SCHEDULER_LOCATION" \
  SERVICE_URL="$URL" CRON_SECRET="$CRON_SECRET" \
  bash "$(dirname "${BASH_SOURCE[0]}")/configure-scheduler.sh"
printf '\nDeployed: %s\n' "$URL"
if [[ "$ADMIN_AUTH_MODE" == channel_admins ]]; then
  printf 'Channel owner and channel administrators with posting rights can open the bot with /start.\n'
elif [[ "$ADMIN_ID" == 0 ]]; then
  printf 'Administrator is not assigned. Telegram admin commands are disabled; collection runs on schedule.\n'
else
  printf 'Open your Telegram admin bot and send /start\n'
fi
