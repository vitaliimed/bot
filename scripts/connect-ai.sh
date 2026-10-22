#!/usr/bin/env bash
set -euo pipefail
set +x
PROJECT_ID=${PROJECT_ID:-winged-ratio-377512}
REGION=${REGION:-europe-north1}
SERVICE=finland-news-bot
CHOICE=${1:-all}
case "$CHOICE" in
  all) providers=(openai gemini groq deepseek mistral openrouter) ;;
  openai|gemini|groq|deepseek|mistral|openrouter) providers=("$CHOICE") ;;
  *) echo 'Usage: bash scripts/connect-ai.sh all|openai|gemini|groq|deepseek|mistral|openrouter'; exit 1 ;;
esac
RUNTIME=$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --format='value(spec.template.spec.serviceAccountName)')
[[ -n "$RUNTIME" ]] || { echo 'Runtime service account missing'; exit 1; }
bindings=()
trap 'unset PROVIDER_KEY' EXIT
for provider in "${providers[@]}"; do
  read -rsp "$provider API key (Enter = keep current/skip): " PROVIDER_KEY
  printf '\n'
  [[ -n "$PROVIDER_KEY" ]] || continue
  secret_name="$provider-api-key"
  variable_name="${provider^^}_API_KEY"
  gcloud secrets describe "$secret_name" --project="$PROJECT_ID" >/dev/null 2>&1 || \
    gcloud secrets create "$secret_name" --project="$PROJECT_ID" --replication-policy=automatic >/dev/null
  printf %s "$PROVIDER_KEY" | gcloud secrets versions add "$secret_name" --project="$PROJECT_ID" --data-file=- >/dev/null
  unset PROVIDER_KEY
  gcloud secrets add-iam-policy-binding "$secret_name" --project="$PROJECT_ID" \
    --member="serviceAccount:$RUNTIME" --role=roles/secretmanager.secretAccessor --quiet >/dev/null
  bindings+=("$variable_name=$secret_name:latest")
done
if ((${#bindings[@]})); then
  joined=$(IFS=,; printf %s "${bindings[*]}")
  gcloud run services update "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --update-secrets="$joined"
  printf 'Keys attached. /settings → AI: select order/models. /ask checks a small text request.\n'
else
  printf 'No new keys entered; existing configuration preserved.\n'
fi
