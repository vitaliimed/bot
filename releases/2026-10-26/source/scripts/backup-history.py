#!/usr/bin/env python3
"""Read-only Firestore JSONL export. No key payloads, mutations, Git or paid AI calls."""
import argparse
import json
import os
import pathlib
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime,timezone

COLLECTIONS=('items','deliveries','story_locks','scheduled_posts','settings','sources','channels','errors','ai_usage','ai_requests','ai_credentials')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',default=os.getenv('PROJECT_ID','winged-ratio-377512'))
    parser.add_argument('--output',default=str(pathlib.Path.home()/'bot-backups'/('history-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S'))))
    args=parser.parse_args()
    os.umask(0o077)
    folder=pathlib.Path(args.output);folder.mkdir(parents=True,exist_ok=False)
    access=subprocess.run(['gcloud','auth','print-access-token'],check=True,capture_output=True,text=True).stdout.strip()
    base='https://firestore.googleapis.com/v1/projects/'+urllib.parse.quote(args.project,safe='')+'/databases/(default)/documents/'
    manifest={'project':args.project,'created_at':datetime.now(timezone.utc).isoformat(),'collections':{},'complete':False}
    (folder/'manifest.json').write_text(json.dumps(manifest,indent=2))
    for collection in COLLECTIONS:
        count=0;page=''
        with (folder/(collection+'.jsonl')).open('w') as stream:
            while True:
                query=urllib.parse.urlencode({'pageSize':100,**({'pageToken':page} if page else {})})
                request=urllib.request.Request(base+collection+'?'+query,headers={'Authorization':'Bearer '+access})
                with urllib.request.urlopen(request,timeout=60) as response:data=json.load(response)
                for document in data.get('documents',[]):
                    name=document['name'].rsplit('/',1)[-1]
                    # Transient dialogs can contain previews or short-lived confirmations; exclude them.
                    if collection=='settings' and name.startswith(('input:','dialog:','manual_send:','mass:','bulk:','reconcile:','sourcecopy:')):continue
                    stream.write(json.dumps(document,ensure_ascii=False)+'\n');count+=1
                page=data.get('nextPageToken','')
                if not page:break
        manifest['collections'][collection]=count
        (folder/'manifest.json').write_text(json.dumps(manifest,indent=2))
        print(collection+': '+str(count))
    manifest['complete']=True
    (folder/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print('Backup saved: '+str(folder))
    print('Private post history included. API key payloads are not exported; ai_credentials contains references only.')

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Backup incomplete: '+type(error).__name__+'. Check access and the manifest. Do not use an incomplete copy for recovery.')
        raise SystemExit(1)
