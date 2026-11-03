#!/usr/bin/env bash
# Conservative live Cloud Run deployment. Never touches git or the user's code checkout.
set -euo pipefail
umask 077
PACK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="winged-ratio-377512"
REGION="europe-north1"
SERVICE="finland-news-bot"
PREV_EXPECTED="${PREV_EXPECTED:-finland-news-bot-00010-cec}"
VER_EXPECTED="${VER_EXPECTED:-v2-2be149db8aad}"
SCHED_LOCATION="europe-west1"
mkdir -p "$HOME/bot-burst-backups"
TS="$(date -u +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d "$HOME/bot-burst-backups/peak50-${TS}-XXXX")"
chmod 700 "$WORK"
echo "Backup and staging: $WORK"
command -v gcloud >/dev/null || { echo 'STOP: gcloud missing'; exit 2; }
command -v python3 >/dev/null || { echo 'STOP: Python missing'; exit 2; }
ROLLED_OVER=0
rollback_on_error() {
  if [[ "$ROLLED_OVER" == '1' ]]; then
    echo 'ERROR AFTER CUTOVER. Reverting service traffic to previous production revision...'
    gcloud run services update-traffic "$SERVICE" --project "$PROJECT" --region "$REGION" \
      --to-revisions "$PREV_EXPECTED=100" || true
  fi
}
trap rollback_on_error ERR
gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format=json > "$WORK/service-before.json"
python3 - "$WORK/service-before.json" "$PREV_EXPECTED" "$VER_EXPECTED" > "$WORK/preflight.txt" <<'PY'
import json,sys
p,revision,version=sys.argv[1:]
s=json.load(open(p));routes=s.get('status',{}).get('traffic',[])
if len(routes)!=1 or routes[0].get('percent')!=100 or routes[0].get('revisionName')!=revision:
    raise SystemExit('STOP: active revision is not '+revision+'; no deployment made.')
env=s.get('spec',{}).get('template',{}).get('spec',{}).get('containers',[{}])[0].get('env',[])
actual=next((e.get('value') for e in env if e.get('name')=='APP_VERSION'),None)
if actual!=version:raise SystemExit('STOP: live version is different; no deployment made: '+str(actual))
print(revision,version)
PY
cat "$WORK/preflight.txt"
# Read-only provenance verification. Must match the actual running OCI digest.
python3 "$PACK/scripts/capture_live_source.py" | tee "$WORK/capture.log"
BUNDLE="$(python3 - "$WORK/capture.log" <<'PY'
from pathlib import Path
import sys
out=''
for line in Path(sys.argv[1]).read_text().splitlines():
    if line.startswith('PASS: source matched by digest; code bundle: '):
        out=line.split('code bundle: ',1)[1].strip()
if not out or not Path(out).is_file():raise SystemExit('STOP: verified source unavailable; nothing deployed')
print(out)
PY
)"
python3 - "$BUNDLE" "$WORK" <<'PY'
from pathlib import Path
import shutil,sys
bundle,work=sys.argv[1:]
extracted=Path(bundle).parent/'extracted'
mains=list(extracted.rglob('app/main.py'))
if len(mains)!=1:raise SystemExit('STOP: original build-source tree must contain exactly one app/main.py')
root=mains[0].parent.parent
out=Path(work)/'source'
if out.exists():raise SystemExit('STOP: staging path already exists')
def ignore(directory, names):
    return [n for n in names if n in ('.git','__pycache__','.venv','.env','.env.local')
            or n.startswith('.secret') or n.endswith('.pyc')]
shutil.copytree(root,out,symlinks=False,ignore=ignore)
if not (out/'app/main.py').exists():raise SystemExit('STOP: missing app/main.py')
print('Verified full Cloud Build source copied to:',out)
PY

# Source zip excludes secret-looking files and dotfiles by design; preserve actual
# included Dockerfile/requirements. App source should remain buildable.
python3 "$PACK/scripts/patch_source.py" "$WORK/source"
python3 -m compileall -q "$WORK/source/app"
(
  cd "$WORK/source"
  PYTHONPATH="$WORK/source:$PACK" python3 -m unittest discover -s "$PACK/tests" -v
)
# Check the remaining capabilities installed in source before any deployment.
python3 - "$WORK/source" <<'PY'
from pathlib import Path
import ast,sys
root=Path(sys.argv[1]);main=(root/'app/main.py').read_text()
needed=('publication_worker','durable_mass_publication','story_uniqueness')
if not all(n in main or any(n in p.read_text(errors='ignore') for p in (root/'app').glob('*.py')) for n in needed):
    raise SystemExit('STOP: expected v2 capabilities absent; no deployment.')
print('V2 features present and staged source compatible.')
PY

if [[ "${1:-}" != "--deploy" ]]; then
    echo "CHECK COMPLETE. NO DEPLOYMENT. Rerun with --deploy to install. Stage: $WORK"
    exit 0
fi

python3 "$PACK/scripts/provision_secrets.py"
RUNTIME="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(spec.template.spec.serviceAccountName)')"
BASE_URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')"
if [[ -z "$RUNTIME" || -z "$BASE_URL" ]]; then echo 'STOP: runtime SA or service URL missing';exit 2;fi
for SECRET in finland-bot-cloudflare-workers-token finland-bot-haimaker-api-key; do
  gcloud secrets add-iam-policy-binding "$SECRET" --project "$PROJECT" \
      --member="serviceAccount:$RUNTIME" --role=roles/secretmanager.secretAccessor --quiet >/dev/null
done
# A candidate revision receives no traffic until its health endpoint is confirmed.
cd "$WORK/source"
echo 'Deploying candidate revision with --no-traffic; existing production traffic stays unchanged.'
gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --source . \
  --no-traffic --tag peak50-candidate --timeout 300 \
  --update-secrets "BOT_HAIMAKER_API_KEY=finland-bot-haimaker-api-key:latest,BOT_CF_API_TOKEN=finland-bot-cloudflare-workers-token:latest" \
  --update-env-vars "BOT_CF_ACCOUNT_ID=b1485cf2910a3ec64d102525b7ec51a6,BOT_CF_IMAGES=1,BOT_CF_IMAGE_DAILY_CAP=10,BOT_BURST_RUNNER_EMAIL=$RUNTIME,BOT_BURST_AUDIENCE=$BASE_URL,APP_VERSION=${VER_EXPECTED}-peak50"
NEW="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.latestCreatedRevisionName)')"
CANDIDATE="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format=json | python3 -c 'import json,sys; print(next((x.get("url","") for x in json.load(sys.stdin).get("status",{}).get("traffic",[]) if x.get("tag")=="peak50-candidate"),""))')"
if [[ -z "$NEW" || -z "$CANDIDATE" ]];then echo 'STOP: candidate unknown, production remains unchanged'; exit 2;fi
python3 - "$CANDIDATE" <<'PY'
import json,sys,urllib.request
url=sys.argv[1]+'/burst50/health'
try:
    with urllib.request.urlopen(url,timeout=25) as r: result=json.load(r)
except Exception as error:raise SystemExit('STOP: candidate health endpoint inaccessible; production unchanged: '+type(error).__name__)
if not result.get('ok') or result.get('addon')!='burst50':
    raise SystemExit('STOP: candidate did not load burst50; production unchanged')
print('Candidate add-on health verified:',result)
PY
# Revalidate that production did not shift while staging.
LIVE="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format=json | python3 -c 'import json,sys; s=json.load(sys.stdin); print(next((t.get("revisionName","") for t in s.get("status",{}).get("traffic",[]) if t.get("percent",0)>0),""))')"
if [[ "$LIVE" != "$PREV_EXPECTED" ]];then echo 'STOP: live traffic changed concurrently'; exit 2;fi
# Explicit exact-revision cutover. Documented rollback is printed, never automatic historical replays.
gcloud run services update-traffic "$SERVICE" --project "$PROJECT" --region "$REGION" --to-revisions "$NEW=100"
ROLLED_OVER=1
printf 'ROLLBACK: gcloud run services update-traffic %s --project %s --region %s --to-revisions %s=100\n' "$SERVICE" "$PROJECT" "$REGION" "$PREV_EXPECTED" | tee "$WORK/ROLLBACK.txt"
echo 'Production revision: ' "$NEW"
# Set scheduler job, checked and signed with authenticated service-account OIDC. Default burst disabled.
if gcloud scheduler jobs describe finland-news-burst50 --project "$PROJECT" --location "$SCHED_LOCATION" >/dev/null 2>&1; then
 echo 'Existing finland-news-burst50 Scheduler job detected; verify its audience and target manually.'
else
  gcloud scheduler jobs create http finland-news-burst50 --project "$PROJECT" --location "$SCHED_LOCATION" \
     --schedule='* * * * *' --time-zone=Europe/Helsinki --uri="$BASE_URL/burst50/tick" \
     --http-method=POST --oidc-service-account-email="$RUNTIME" --oidc-token-audience="$BASE_URL" \
     --attempt-deadline=180s
fi
ROLLED_OVER=0
trap - ERR
echo 'SUCCESS: addon deployed; disabled until /burst50 start or /burst50 auto on. Backups: ' "$WORK"
echo 'Telegram: /burst50 status | /burst50 start | /burst50 stop | /burst50 auto on | /burst50 auto off'
