#!/usr/bin/env bash
# Safe staging: use the exact v2 ZIP; never touch the GitHub repository.
set -euo pipefail
BASE="$HOME/projects/bot"
V2="$BASE/finland-news-bot-v2-20261025.zip"
HERE="$(cd "$(dirname "$0")" && pwd)"
[[ -f "$V2" ]] || { echo "Missing deployed-source archive: $V2"; exit 1; }
[[ -f "$HERE/app/ua_language_guard.py" ]] || { echo "Missing guard module"; exit 1; }
BUILD="$(mktemp -d "$BASE/ua-lang-stage-XXXXXXXX")"
echo "Staging directory: $BUILD"
unzip -q "$V2" -d "$BUILD"
SRC="$BUILD/finland-news-bot-v2"
[[ -f "$SRC/app/main.py" && -f "$SRC/scripts/upgrade-all.sh" ]] || {
 echo "Unexpected source layout. Aborting without deployment."; exit 1;
}
cp "$HERE/app/ua_language_guard.py" "$SRC/app/ua_language_guard.py"
cp "$HERE/apply_ua_language_fix.py" "$SRC/apply_ua_language_fix.py"
cp "$HERE/tests/test_ua_language_guard.py" "$SRC/tests/test_ua_language_guard.py"
cd "$SRC"
python3 apply_ua_language_fix.py --check
python3 -m unittest discover -s tests -p 'test_ua_language_guard.py' -v
python3 apply_ua_language_fix.py --apply
python3 -m compileall -q app
python3 -m unittest discover -s tests -v
PROJECT_ID=winged-ratio-377512 REGION=europe-north1 EVENTS_CHANNEL_ID=@eventhsl bash scripts/upgrade-all.sh
bash scripts/diagnose.sh
printf '\nDeployment finished. Guard defaults to UA_TRANSLATION_MODE=off.\n'
printf 'The staging source and backups are in %s\n' "$SRC"
