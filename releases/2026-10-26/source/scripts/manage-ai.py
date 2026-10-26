"""Connect runtime AI keys from Cloud Shell without sending them through Telegram."""
import getpass
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.credentials import validate_key, validate_provider

PROJECT = os.getenv('PROJECT_ID','winged-ratio-377512')
REGION = os.getenv('REGION','europe-north1')


def gcloud(*args, data=None):
    result = subprocess.run(['gcloud',*args,'--project='+PROJECT],input=data,capture_output=True,text=True)
    if result.returncode:
        raise RuntimeError('gcloud operation failed; check the account/project permissions')
    return result.stdout.strip()


def request(url, data=None, headers=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url,body,dict({'Content-Type':'application/json'},**(headers or {})),method=method)
    with urllib.request.urlopen(req,timeout=20) as response:
        return json.load(response)


def main():
    choice = sys.argv[1] if len(sys.argv)>1 else 'all'
    providers = ['openai','gemini','groq','deepseek','mistral','openrouter','cloudflare','huggingface'] if choice=='all' else [validate_provider(choice)]
    url = gcloud('run','services','describe','finland-news-bot','--region='+REGION,'--format=value(status.url)')
    if 'runtime_ai_keys' not in request(url+'/health').get('features',[]):
        raise RuntimeError('First run bash scripts/upgrade-all.sh, then connect keys')
    # Create only the dedicated AI secrets. Runtime receives no project-wide secret administration role.
    subprocess.run(['bash',str(Path(__file__).with_name('configure-ai-keys.sh')),*providers],check=True)
    connected = []
    for provider in providers:
        value = getpass.getpass(provider+' API key (Enter = preserve current/skip): ').strip()
        if not value: continue
        try:
            validate_key(value)
        except ValueError:
            print(provider+': invalid key format; not saved')
            continue
        version = gcloud('secrets','versions','add','bot-ai-'+provider,'--data-file=-','--format=value(name)',data=value)
        value = None
        if not version.startswith('projects/'+PROJECT+'/'):
            # gcloud may return a project number; use the known project ID in the stored reference.
            version = f'projects/{PROJECT}/secrets/bot-ai-{provider}/versions/{version.rsplit("/",1)[-1]}'
        token = gcloud('auth','print-access-token')
        fields = {'mode':{'stringValue':'secret'},'version':{'stringValue':version},
                  'updated_by':{'stringValue':'cloudshell'},'updated_at':{'timestampValue':datetime.now(timezone.utc).isoformat()}}
        request(f'https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents/ai_credentials/{provider}',
                {'fields':fields},{'Authorization':'Bearer '+token},method='PATCH')
        token = None
        connected.append(provider)
        print(provider+': connected; the server does not need a restart')
    print('Connected: '+(', '.join(connected) or 'no changes')+'. Open /ai in the bot and use the test button.')


if __name__=='__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\nInput cancelled. Completed connections are preserved.')
        sys.exit(1)
    except Exception as error:
        # Never print HTTP response bodies, subprocess stderr, keys or access tokens.
        print(str(error) if isinstance(error,RuntimeError) else 'Key connection failed: '+type(error).__name__)
        sys.exit(1)
