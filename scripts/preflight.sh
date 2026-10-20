#!/usr/bin/env bash
set -euo pipefail
python3 -m compileall -q app
for tool in gcloud curl openssl; do command -v "$tool" >/dev/null || { echo "Missing: $tool"; exit 1; }; done
: "${PROJECT_ID:?Set PROJECT_ID}"
: "${ADMIN_ID:=0}"
: "${CHANNEL_ID:?Set CHANNEL_ID}"
[[ "$ADMIN_ID" =~ ^[0-9]+$ ]] || { echo 'ADMIN_ID must be numeric'; exit 1; }
[[ "$CHANNEL_ID" == @* || "$CHANNEL_ID" == -100* ]] || { echo 'CHANNEL_ID must be @public_channel or -100...'; exit 1; }
gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null
ACCOUNT=$(gcloud auth list --filter='status:ACTIVE' --format='value(account)' | head -1)
[[ -n "$ACCOUNT" ]] || { echo 'gcloud auth missing'; exit 1; }
echo "Preflight OK. Google account: $ACCOUNT; project: $PROJECT_ID"
