#!/usr/bin/env bash
set -euo pipefail
set +x
PROJECT_ID=${PROJECT_ID:-winged-ratio-377512}
REGION=${REGION:-europe-north1}
RUNTIME=${RUNTIME:-$(gcloud run services describe finland-news-bot --project="$PROJECT_ID" --region="$REGION" --format='value(spec.template.spec.serviceAccountName)')}
[[ -n "$RUNTIME" ]] || { echo 'Runtime service account missing'; exit 1; }
providers=("$@")
((${#providers[@]})) || providers=(openai gemini groq deepseek mistral openrouter cloudflare huggingface)
for provider in "${providers[@]}"; do
  case "$provider" in openai|gemini|groq|deepseek|mistral|openrouter|cloudflare|huggingface) ;; *) echo 'Unknown provider'; exit 1 ;; esac
  managed_secret="bot-ai-$provider"
  if ! gcloud secrets describe "$managed_secret" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud secrets create "$managed_secret" --project="$PROJECT_ID" --replication-policy=automatic >/dev/null
  fi
  for role in secretAccessor secretVersionAdder; do
    gcloud secrets add-iam-policy-binding "$managed_secret" --project="$PROJECT_ID" \
      --member="serviceAccount:$RUNTIME" --role="roles/secretmanager.$role" --quiet >/dev/null
  done
done
printf 'AI key storage prepared. Existing keys are preserved.\n'
