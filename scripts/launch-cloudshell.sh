#!/usr/bin/env bash
set -euo pipefail
set +x

cd "$(dirname "${BASH_SOURCE[0]}")/.."

export PROJECT_ID="${PROJECT_ID:-winged-ratio-377512}"
export REGION="${REGION:-europe-north1}"
export CHANNEL_ID="${CHANNEL_ID:-@suominewsua}"
export ADMIN_ID="${ADMIN_ID:-0}"
export GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.5-flash-lite}"

cleanup() {
  unset BOT_TOKEN GEMINI_API_KEY WEBHOOK_SECRET CRON_SECRET
}
trap cleanup EXIT

bash scripts/preflight.sh
printf '\nProject: %s\nRegion: %s\nChannel: %s\nModel: %s\nAdmin ID: %s\n\n' \
  "$PROJECT_ID" "$REGION" "$CHANNEL_ID" "$GEMINI_MODEL" "$ADMIN_ID"

if [[ -z "${BOT_TOKEN:-}" ]]; then
  read -r -s -p 'Telegram bot token: ' BOT_TOKEN
  printf '\n'
fi
if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  read -r -s -p 'Gemini API key: ' GEMINI_API_KEY
  printf '\n'
fi
: "${BOT_TOKEN:?Telegram bot token is required}"
: "${GEMINI_API_KEY:?Gemini API key is required}"
export BOT_TOKEN GEMINI_API_KEY
export WEBHOOK_SECRET="${WEBHOOK_SECRET:-$(openssl rand -hex 24)}"
export CRON_SECRET="${CRON_SECRET:-$(openssl rand -hex 24)}"

bash scripts/deploy.sh
