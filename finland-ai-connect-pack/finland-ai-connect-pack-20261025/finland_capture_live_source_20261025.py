#!/usr/bin/env python3
"""Read-only recover the source tied by OCI digest to the LIVE Cloud Run revision.

Never replaces ~/projects/bot, changes Git, or deploys. If Cloud Build provenance is
not sufficient to prove a match, fail closed rather than using the wrong source.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone

PROJECT='winged-ratio-377512';REGION='europe-north1';SERVICE='finland-news-bot'


def run(*args,timeout=90):
    try:p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)
    except Exception as e:raise RuntimeError(type(e).__name__) from None
    if p.returncode:raise RuntimeError(args[0]+' failed ('+str(p.returncode)+') '+p.stderr.strip()[:150])
    return p.stdout.strip()


def json_cmd(*args):return json.loads(run(*args))


def normalize_digest(v):
    v=str(v or '').lower().strip()
    if '@sha256:' in v:v='sha256:'+v.split('@sha256:')[-1]
    if v.startswith('sha256:') and len(v)==71:return v
    return ''


def pick_live_revision(s):
    traffic=[t for t in s.get('status',{}).get('traffic',[]) if t.get('percent',0)>0]
    if len(traffic)!=1 or traffic[0].get('percent')!=100:
        raise RuntimeError('Traffic is split or unknown; refusing to infer active source')
    rev=traffic[0].get('revisionName') or s.get('status',{}).get('latestReadyRevisionName')
    if not rev:raise RuntimeError('Live revision missing')
    return rev


def source_of_build(b):
    for candidate in (b.get('source',{}).get('storageSource',{}),
                      b.get('sourceProvenance',{}).get('resolvedStorageSource',{})):
        bucket=candidate.get('bucket');obj=candidate.get('object')
        if bucket and obj:return 'gs://'+bucket+'/'+obj
    return ''


def safe_extract(archive,target):
    # Cloud Build's ordinary --source archive is tar.gz; reject symlinks/unsafe paths.
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive,'r:*') as t:
            for member in t.getmembers():
                p=Path(member.name)
                if p.is_absolute() or '..' in p.parts or not (member.isfile() or member.isdir()):
                    raise RuntimeError('Unsafe archive member refused')
            t.extractall(target,filter='data')
    elif zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for member in z.infolist():
                p=Path(member.filename)
                if p.is_absolute() or '..' in p.parts or member.external_attr>>16 & 0o170000==0o120000:
                    raise RuntimeError('Unsafe ZIP member refused')
            z.extractall(target)
    else:raise RuntimeError('Unsupported Cloud Build source format')


def bundle_sources(root,output):
    whitelist={'Dockerfile','requirements.txt','.dockerignore','.gcloudignore'}
    count=0
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for p in root.rglob('*'):
            if not p.is_file():continue
            rel=p.relative_to(root)
            if rel.parts[0] not in ('app','scripts','tests') and str(rel) not in whitelist:continue
            if p.suffix not in ('.py','.sh','.txt','.toml','.json','.md') and str(rel) not in whitelist:continue
            if any(seg in ('__pycache__','.git','.venv','venv','.env') for seg in rel.parts):continue
            if ('secret' in p.name.lower() or 'credentials' in p.name.lower()):continue
            if p.stat().st_size>1_000_000:continue
            z.write(p,arcname=str(rel));count+=1
    return count


def main():
    print('=== Live Cloud Run source: READ ONLY, do not overwrite working repo ===')
    service=json_cmd('gcloud','run','services','describe',SERVICE,'--project',PROJECT,'--region',REGION,'--format=json')
    rev=pick_live_revision(service)
    r=json_cmd('gcloud','run','revisions','describe',rev,'--project',PROJECT,'--region',REGION,'--format=json')
    containers=r.get('spec',{}).get('containers',[])
    if not containers:containers=r.get('spec',{}).get('template',{}).get('spec',{}).get('containers',[])
    digest=normalize_digest(r.get('status',{}).get('imageDigest'))
    if not digest:
        digest=next((normalize_digest(c.get('image')) for c in containers if normalize_digest(c.get('image'))),'')
    print('Live revision:',rev)
    print('Live container digest:',digest or '(not exposed)')
    if not digest:
        print('STOP: no immutable live container digest; cannot safely match a Cloud Build source')
        return 2
    builds=[]
    for location in (REGION,'global'):
        try:
            builds+=json_cmd('gcloud','builds','list','--project',PROJECT,'--region',location,
                             '--limit=100','--format=json')
        except Exception:
            pass
    seen=set();matched=[]
    for b in builds:
        if b.get('id') in seen:continue
        seen.add(b.get('id'))
        if b.get('status')!='SUCCESS':continue
        results=b.get('results',{}).get('images',[]) or []
        digests={normalize_digest(x.get('digest')) for x in results}
        if digest in digests and source_of_build(b):matched.append(b)
    if len(matched)!=1:
        print('STOP: live digest matches',len(matched),'Cloud Build source archives; need provenance check')
        print('Candidate build IDs:',[b.get('id') for b in matched][:5])
        print('No deployment or changes performed.')
        return 2
    build=matched[0]
    timestamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    output=Path.home()/('bot-live-source-'+timestamp)
    output.mkdir(mode=0o700)
    archive=output/'original-cloud-build-source'
    src=source_of_build(build)
    print('Verified matching Cloud Build:',build.get('id'))
    print('Downloading immutable source archive into:',output)
    run('gcloud','storage','cp',src,str(archive),timeout=120)
    unpack=output/'extracted'
    unpack.mkdir()
    safe_extract(archive,unpack)
    mains=list(unpack.rglob('app/main.py'))
    if len(mains)!=1:
        print('STOP: expected exactly one app/main.py; found',len(mains))
        return 2
    root=mains[0].parent.parent
    p=output/'review-source.zip'
    n=bundle_sources(root,p)
    if n<4:raise RuntimeError('Source package unexpectedly small')
    report={'project':PROJECT,'region':REGION,'revision':rev,'digest':digest,
            'build_id':build.get('id'),'files':n,'bundle':str(p),
            'note':'Review the bundle for accidental hardcoded secrets before sharing.'}
    (output/'provenance.json').write_text(json.dumps(report,indent=2))
    print('PASS: source matched by digest; code bundle:',p)
    print('Source files in package:',n)
    print('IMPORTANT: do not deploy this archive directly; live service is unchanged.')
    print('Review package for secrets before sharing.')
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except Exception as e:
        print('STOP:',str(e)[:250]);sys.exit(2)
