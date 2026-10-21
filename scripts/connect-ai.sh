#!/usr/bin/env bash
# Connect an optional writer/reviewer without placing keys in shell history or Git.
set -euo pipefail
set +x
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PROJECT_ID=${PROJECT_ID:-winged-ratio-377512}
REGION=${REGION:-europe-north1}
PROVIDER=${1:-}
case "$PROVIDER" in
  groq) SECRET=groq-api-key; VARIABLE=GROQ_API_KEY ;;
  openai) SECRET=openai-api-key; VARIABLE=OPENAI_API_KEY ;;
  *) echo 'Usage: bash scripts/connect-ai.sh groq|openai'; exit 1 ;;
esac
SERVICE=finland-news-bot
RUNTIME=$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION" \
  --format='value(spec.template.spec.serviceAccountName)')
[[ -n "$RUNTIME" ]] || { echo 'Existing runtime service account is required'; exit 1; }
read -rsp "$PROVIDER API key: " PROVIDER_KEY
printf '\n'
[[ -n "$PROVIDER_KEY" ]] || { echo 'Empty key'; exit 1; }
trap 'unset PROVIDER_KEY' EXIT
gcloud secrets describe "$SECRET" --project="$PROJECT_ID" >/dev/null 2>&1 || \
  gcloud secrets create "$SECRET" --project="$PROJECT_ID" --replication-policy=automatic
printf %s "$PROVIDER_KEY" | gcloud secrets versions add "$SECRET" --project="$PROJECT_ID" --data-file=- >/dev/null
gcloud secrets add-iam-policy-binding "$SECRET" --project="$PROJECT_ID" \
  --member="serviceAccount:$RUNTIME" --role=roles/secretmanager.secretAccessor --quiet >/dev/null
unset PROVIDER_KEY
gcloud run services update "$SERVICE" --project="$PROJECT_ID" --region="$REGION" \
  --update-secrets="$VARIABLE=$SECRET:latest"
printf '%s configured. Open /settings to select writer order, model and reviewer.\n' "$PROVIDER"
