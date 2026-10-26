"""Cloud Shell backup + read-only queue audit, using existing gcloud credentials."""
import json,os,subprocess,urllib.request,urllib.parse
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter

PROJECT=os.getenv('PROJECT_ID','winged-ratio-377512')
REGION=os.getenv('REGION','europe-north1')

def command(*args):
    result=subprocess.run(['gcloud',*args],capture_output=True,text=True,check=True)
    return result.stdout.strip()

def decode(value):
    if 'stringValue' in value:return value['stringValue']
    if 'integerValue' in value:return int(value['integerValue'])
    if 'booleanValue' in value:return value['booleanValue']
    if 'doubleValue' in value:return value['doubleValue']
    if 'timestampValue' in value:return value['timestampValue']
    if 'mapValue' in value:return {k:decode(v) for k,v in value['mapValue'].get('fields',{}).items()}
    if 'arrayValue' in value:return [decode(v) for v in value['arrayValue'].get('values',[])]
    return None

def documents(collection,token,fields=()):
    result=[];page=''
    base=f'https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents/{collection}'
    while True:
        params=[('pageSize','300')]+[('mask.fieldPaths',key) for key in fields]
        if page:params.append(('pageToken',page))
        req=urllib.request.Request(base+'?'+urllib.parse.urlencode(params),headers={'Authorization':'Bearer '+token})
        with urllib.request.urlopen(req,timeout=30) as response:value=json.load(response)
        result.extend(value.get('documents',[]));page=value.get('nextPageToken','')
        if not page:return result

def main():
    token=command('auth','print-access-token')
    folder=Path(os.getenv('BOT_BACKUP_DIR',str(Path.home()/'bot-backups'/datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S'))))
    folder.mkdir(parents=True,exist_ok=True);folder.chmod(0o700)
    service=json.loads(command('run','services','describe','finland-news-bot','--project='+PROJECT,'--region='+REGION,'--format=json'))
    # Preserve the full service configuration locally, without printing environment values.
    records={'service':service}
    for collection in ('settings','sources','channels'):
        rows=documents(collection,token)
        if collection=='settings':rows=[r for r in rows if r['name'].rsplit('/',1)[-1].startswith(('editor','autopublish','logo'))]
        records[collection]=rows
    path=folder/'settings-and-service.json';path.write_text(json.dumps(records,ensure_ascii=False,indent=2));path.chmod(0o600)
    import shlex
    traffic=','.join(t['revisionName']+'='+str(t['percent']) for t in service['status'].get('traffic',[]) if t.get('percent',0))
    rollback=folder/'rollback.sh'
    rollback.write_text('#!/usr/bin/env bash\nset -euo pipefail\n'+shlex.join(['gcloud','run','services','update-traffic','finland-news-bot','--project='+PROJECT,'--region='+REGION,'--to-revisions='+traffic])+'\n')
    rollback.chmod(0o700)
    print('Backup saved:',path)
    print('Rollback command: bash',rollback)
    print('Served traffic:',service.get('status',{}).get('traffic',[]))
    settings={r['name'].rsplit('/',1)[-1]:decode({'mapValue':{'fields':r.get('fields',{})}}).get('value') for r in records['settings']}
    fields=('route','status','category','reason','last_publish_reason','image_error','ai_provider','translation_missing','published_at')
    items=documents('items',token,fields)
    for route in ('news','events'):
        values=[decode({'mapValue':{'fields':r.get('fields',{})}}) for r in items]
        values=[v for v in values if (v.get('route') or ('events' if v.get('category')=='events' else 'news'))==route]
        suffix='_events' if route=='events' else ''
        print('Queue:',route,'auto:',settings.get('autopublish'+suffix,False),'statuses:',dict(Counter(v.get('status','unknown') for v in values)))
        print('Editor settings:',json.dumps(settings.get('editor'+suffix,{}),ensure_ascii=False))
        for field in ('reason','last_publish_reason','image_error'):
            print(route,field,dict(Counter(str(v[field])[:200] for v in values if v.get(field))))
    print('Backup excludes message content, publication history and API key payloads. History remains in Firestore.')

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Backup/audit failed:',type(error).__name__,'Check Firestore read permissions and active gcloud account.');raise SystemExit(1)
