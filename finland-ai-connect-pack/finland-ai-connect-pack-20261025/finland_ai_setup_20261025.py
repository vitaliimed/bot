#!/usr/bin/env python3
"""Safe AI provider verification and optional secure Haimaker key provisioning.

Targets user's existing Cloud Shell project, no Telegram posts, no code deployment, no Firestore writes.
Cloudflare token is resolved read-only from previous audit's known bot Secret Manager references.
"""
from __future__ import annotations
import argparse
import base64
from datetime import datetime, timezone
import getpass
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

PROJECT='winged-ratio-377512'
REGION='europe-north1'
SERVICE='finland-news-bot'
CF_ID_DEFAULT='b1485cf2910a3ec64d102525b7ec51a6'
HAI_SECRET='finland-bot-haimaker-api-key'
HAI_MODEL='z-ai/glm-5.3'
HAI_FREE_END=datetime(2026,11,1,14,0,0,tzinfo=timezone.utc)
TEST_TEXT=('Helsingissä järjestetään maksuton perhetapahtuma lauantaina 31. lokakuuta 2026 '
           'klo 12. Tapahtumapaikka on keskustakirjasto Oodi.')
IMAGE_PROMPT=('Flat editorial illustration of a Helsinki tram in autumn, vibrant autumn leaves, '
              'minimal clean composition, no words, no lettering, no watermarks, no logos.')


def redact(s):
    s = str(s)
    s = re.sub(r'(sk-[A-Za-z0-9_-]{6,}|AIza[A-Za-z0-9_-]{15,}|hf_[A-Za-z0-9_-]{8,})','[REDACTED]',s)
    s = re.sub(r'(?i)(bearer\s+)[^\s,]+',r'\1[REDACTED]',s)
    return s[:180].replace('\n',' ')


def shell(*args, stdin=None, timeout=50, allow_error=False):
    try:
        p=subprocess.run(args,input=stdin,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                         text=True,timeout=timeout)
    except Exception as e:
        raise RuntimeError(type(e).__name__) from None
    if p.returncode and not allow_error:
        raise RuntimeError('command returned '+str(p.returncode)+' ('+str(args[0])+') '+redact(p.stderr))
    return p


def json_command(*cmd): return json.loads(shell(*cmd).stdout)


def load_audit():
    p=Path.cwd()/'finland_all_ai_test_20261025.py'
    if not p.is_file():
        print('NOTE: previous audit script not found; Cloudflare token auto-discovery unavailable')
        return None
    spec=importlib.util.spec_from_file_location('original_finland_ai_audit',p)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve_cf_key(audit,service):
    if audit is None: return ''
    try:
        env=audit.env_map(service)
        key,_=audit.gcloud_keys(env,service,'cloudflare')
        if key:return key
        access=audit.run('gcloud','auth','print-access-token')
        docs=audit.scan_possible_ai_settings(access)
        key,_=audit.firestore_key_guess('cloudflare',docs)
        return key
    except Exception as e:
        print('Cloudflare credential resolution unavailable:',type(e).__name__)
        return ''


def http_post(url, key, body, *, timeout=65):
    data=json.dumps(body,ensure_ascii=False).encode('utf-8')
    req=urllib.request.Request(url,data=data,headers={
        'Authorization':'Bearer '+key,'Content-Type':'application/json','Accept':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=timeout) as resp:
            ct=resp.headers.get('Content-Type','')
            raw=resp.read(8000000)
            if 'image/' in ct:return {'_image':raw}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            j=json.loads(e.read(4096))
            d=j.get('error',j) if isinstance(j,dict) else {}
            if isinstance(d,dict):
                detail=str(d.get('message') or d.get('error') or d.get('code') or '')
            else:detail=str(d)
        except Exception:detail=''
        raise RuntimeError('HTTP '+str(e.code)+' '+redact(detail)) from None
    except urllib.error.URLError as e:
        raise RuntimeError('NETWORK '+type(e.reason).__name__) from None
    except TimeoutError:
        raise RuntimeError('TIMEOUT') from None


def ukrainian(text):
    letters=[c for c in text if c.isalpha()]
    ratio=sum('\u0400'<=c<='\u052f' for c in letters)/max(len(letters),1)
    ua=any(ch in text.lower() for ch in 'іїєґ')
    return ratio>=0.65 and ua and '31' in text and '12' in text


def test_haimaker(key,model):
    out=http_post('https://api.haimaker.ai/v1/chat/completions',key,{
        'model':model,'messages':[{'role':'user','content':
        'Переклади цей текст українською, без вигаданих фактів; збережи число 31 і час 12: '
        +TEST_TEXT}], 'max_tokens':250,'temperature':0.1})
    message=out['choices'][0]['message']['content']
    if isinstance(message,list):message=' '.join(str(x.get('text','')) for x in message if isinstance(x,dict))
    if not message or not ukrainian(str(message)):
        raise RuntimeError('Translation returned but Ukrainian/fact check failed')
    print('  Haimaker PASS / Ukrainian translation / model:',model)
    print('  Preview:',str(message).replace('\n',' ')[:100])
    return {'pass':True,'model':model,'preview':str(message)[:120]}


def test_cf_text(key,account):
    out=http_post('https://api.cloudflare.com/client/v4/accounts/'+account+
                  '/ai/run/@cf/meta/llama-3.1-8b-instruct',key,
        {'prompt':'Переклади українською, збережи дату і час. Тільки переклад: '+TEST_TEXT,
         'max_tokens':280},timeout=70)
    if not out.get('success',True):raise RuntimeError('Cloudflare API rejected the call: '+str([e.get('code') for e in out.get('errors',[]) if isinstance(e,dict)]))
    msg=out.get('result',{}).get('response','')
    if not msg or not ukrainian(str(msg)):
        raise RuntimeError('Cloudflare response not verified as Ukrainian or missed date/time')
    print('  Cloudflare TEXT PASS / Ukrainian translation')
    print('  Preview:',str(msg).replace('\n',' ')[:100])
    return {'pass':True,'model':'@cf/meta/llama-3.1-8b-instruct'}


def test_cf_image(key,account,outdir):
    out=http_post('https://api.cloudflare.com/client/v4/accounts/'+account+
                  '/ai/run/@cf/black-forest-labs/flux-1-schnell',key,
                  {'prompt':IMAGE_PROMPT,'steps':4},timeout=90)
    if not out.get('success',True):raise RuntimeError('Cloudflare image rejected: '+str([e.get('code') for e in out.get('errors',[]) if isinstance(e,dict)]))
    img=out.get('_image')
    if img is None:img=base64.b64decode(out['result']['image'],validate=True)
    if len(img)<1024:raise RuntimeError('Unusually small image')
    ext='png' if img[:8]==b'\x89PNG\r\n\x1a\n' else 'jpg'
    path=outdir/('cloudflare_flux.'+ext)
    path.write_bytes(img)
    print('  Cloudflare IMAGE PASS / FLUX.1-schnell / bytes:',len(img))
    print('  Saved locally:',path)
    return {'pass':True,'bytes':len(img),'file':str(path)}


def existing_hai_secret():
    p=shell('gcloud','secrets','versions','access','latest','--secret',HAI_SECRET,
        '--project',PROJECT,allow_error=True)
    return p.stdout.strip() if p.returncode==0 else ''


def store_hai_secret(key):
    if not re.fullmatch('[ -~]{24,300}',key):
        raise RuntimeError('Invalid key format; no storage performed')
    exists=shell('gcloud','secrets','describe',HAI_SECRET,'--project',PROJECT,
                 '--format=value(name)',allow_error=True)
    if exists.returncode:
        shell('gcloud','secrets','create',HAI_SECRET,'--project',PROJECT,
              '--replication-policy=automatic',timeout=45)
    # Pass only via STDIN; never on command line or output.
    shell('gcloud','secrets','versions','add',HAI_SECRET,'--project',PROJECT,
          '--data-file=-',stdin=key,timeout=50)
    print('  Haimaker key saved securely in Secret Manager:',HAI_SECRET)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--cloudflare-account-id',default=CF_ID_DEFAULT)
    ap.add_argument('--save-haimaker-key',action='store_true',help='prompt without echo, test free model, then store in Secret Manager')
    ap.add_argument('--test-images',action='store_true',help='one Cloudflare FLUX call (may consume daily quota)')
    ap.add_argument('--allow-paid-haimaker-test',action='store_true',help='allow one Haimaker text request after free promotion end')
    ap.add_argument('--haimaker-model',default=HAI_MODEL)
    args=ap.parse_args()
    if not re.fullmatch(r'[0-9a-fA-F]{32}',args.cloudflare_account_id):
        ap.error('Cloudflare Account ID must be 32 hex characters')
    if not re.fullmatch(r'[a-zA-Z0-9._/-]+',args.haimaker_model):ap.error('Invalid model')
    if not args.allow_paid_haimaker_test and args.haimaker_model not in (
            'z-ai/glm-5.3','deepseek/deepseek-v4.1-flash','minimax/minimax-m3'):
        ap.error('Refusing unverified non-promotional model without --allow-paid-haimaker-test')
    if datetime.now(timezone.utc)>=HAI_FREE_END and not args.allow_paid_haimaker_test:
        print('NOTICE: Haimaker free promo has ended. Skipping paid API calls unless explicitly authorized.')
        skip_hai=True
    else:skip_hai=False

    d=Path.home()/'bot-ai-audits'/('connect-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S'))
    d.mkdir(parents=True,exist_ok=True)
    report={'phase':'safe_connect','project':PROJECT,'service':SERVICE,
            'cloudflare_account_provided':True,'haimaker':{},'cloudflare_text':{},'cloudflare_image':{}}
    print('=== Finland bot: Haimaker + Cloudflare verification ===')
    print('No Cloud Run changes, no Telegram publication, no Git changes, no Firestore writes.')
    print('Report:',d/'report.json')
    try:
        audit=load_audit()
        service=json_command('gcloud','run','services','describe',SERVICE,
                             '--project',PROJECT,'--region',REGION,'--format=json')
        traffic=[(t.get('revisionName'),t.get('percent')) for t in service.get('status',{}).get('traffic',[])]
        print('  Live traffic:',traffic)
        print('  Live version:',next((e.get('value') for e in service.get('spec',{}).get('template',{}).get('spec',{}).get('containers',[{}])[0].get('env',[]) if e.get('name')=='APP_VERSION'),'not reported'))
    except Exception as e:
        print('CLOUD RUN PREFLIGHT FAIL:',redact(e));return 2

    hai_key=existing_hai_secret()
    if args.save_haimaker_key:
        hai_key=getpass.getpass('Haimaker All Models API key (hidden, not logged): ').strip()
    if not hai_key:
        report['haimaker']={'result':'NO_KEY','next':'Run with --save-haimaker-key'}
        print('  Haimaker: NO_KEY. Run with --save-haimaker-key to enter the key securely.')
    elif skip_hai:
        report['haimaker']={'result':'SKIPPED_PROMO_ENDED'}
        print('  Haimaker: SKIPPED (promo period ended)')
    else:
        try:
            report['haimaker']=test_haimaker(hai_key,args.haimaker_model)
            if args.save_haimaker_key:store_hai_secret(hai_key)
        except Exception as e:
            report['haimaker']={'result':'FAIL','error':redact(e)}
            print('  Haimaker FAIL:',redact(e))
            print('  Key not stored if the test or Secret Manager storage failed.')

    cf_key=resolve_cf_key(audit,service)
    if not cf_key:
        print('  Cloudflare: SKIP (bot token inaccessible to Cloud Shell audit)')
        report['cloudflare_text']={'result':'SKIP_TOKEN'}
    else:
        print('  Cloudflare: token present in existing bot credentials (not printed)')
        try:report['cloudflare_text']=test_cf_text(cf_key,args.cloudflare_account_id)
        except Exception as e:
            report['cloudflare_text']={'result':'FAIL','error':redact(e)}
            print('  Cloudflare TEXT FAIL:',redact(e))
        if args.test_images:
            try:report['cloudflare_image']=test_cf_image(cf_key,args.cloudflare_account_id,d)
            except Exception as e:
                report['cloudflare_image']={'result':'FAIL','error':redact(e)}
                print('  Cloudflare IMAGE FAIL:',redact(e))
        else:print('  Cloudflare image: SKIP; use --test-images for one test image')

    report['note']='Standalone API checks only. Existing deployed bot does not automatically learn a new provider from this script.'
    (d/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('=== SUMMARY ===')
    for k in ('haimaker','cloudflare_text','cloudflare_image'):
        r=report.get(k,{})
        print(' ',k, 'PASS' if r.get('pass') else r.get('result','NOT_REQUESTED'))
    print('No keys printed or stored in report. Do not share a raw key.')
    return 0

if __name__=='__main__':sys.exit(main())
