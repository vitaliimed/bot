#!/usr/bin/env bash
set -euo pipefail
set +x
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PROJECT_ID=${PROJECT_ID:-winged-ratio-377512}
export REGION=${REGION:-europe-north1}
exec python3 scripts/manage-ai.py "${1:-all}"
