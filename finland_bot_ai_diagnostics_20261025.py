#!/usr/bin/env python3
"""Read-only Cloud Run/Firestore diagnosis and explicit live API tests.

Run in Cloud Shell as owner of the Google Cloud project. No deployment, no Telegram posts,
no settings changes. API keys are never printed or written to the report.
"""
import argparse
import base64
import collections
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

PROJECT = 'winged-ratio-377512'
REGION = 'europe-north1'
SERVICE = 'finland-news-bot'
TIMEOUT = 40
DEFAULTS = {
    'gemini_text': 'gemini-3.5-flash-lite',
    'openai_text': 'gpt-4.1-mini',
    'gemini_image': 'gemini-3.1-flash-image',
    'openai_image': 'gpt-image-2.5-flare',
}
IMAGE_PROMPT = ('Create one simple clean flat-style editorial illustration of Helsinki in autumn, '
                'with a tram and yellow leaves, no logos, no people, no text, landscape composition.')
FINNISH_EXAMPLE = 'Helsingissä järjestetään maksuton perhetapahtuma lauantaina 31. lokakuuta klo 12. Tapahtumapaikka on keskustakirjasto Oodi.'
SECRET_NAMES = ('GEMINI_API_KEY','OPENAI_API_KEY')


def run(*args, timeout=40):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'{args[0]} failed: {type(exc).__name__}') from exc
    if p.returncode:
        raise RuntimeError(f'{args[0]} exited {p.returncode}: ' + p.stderr.strip()[:160].splitlines()[0] if p.stderr.strip() else f'{args[0]} exited {p.returncode}')
    return p.stdout.strip()


def get_json_from_cmd(*args):
    return json.loads(run(*args))


def api(url, *, token=None, api_key=None, payload=None, timeout=50, api_kind=''):
    headers = {'Accept': 'application/json'}
    if token: headers['Authorization'] = 'Bearer '+token
    if api_key:
        if api_kind=='gemini': headers['x-goog-api-key'] = api_key
        else: headers['Authorization'] = 'Bearer '+api_key
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method='POST' if data is not None else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read(2048).decode('utf-8', 'replace')
        message = ''
        code = ''
        try:
            info = json.loads(raw).get('error', {})
            if isinstance(info,dict):
                message = str(info.get('message',''))
                code = str(info.get('code',''))
                code = str(info.get('type') or code)
        except (ValueError,TypeError): pass
        # Never expose API request body or credentials in logs.
        msg = re.sub(r'(?i)(sk-[\w-]+|AIza[\w-]+)', '[REDACTED]', message)
        raise RuntimeError(f'HTTP {e.code} {code}: {msg[:180]}') from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f'network {type(e).__name__}: {str(e.reason)[:100] if hasattr(e,"reason") else "timeout"}') from None


def firestore_unwrap(x):
    if not isinstance(x,dict): return x
    if 'stringValue' in x: return x['stringValue']
    if 'booleanValue' in x: return x['booleanValue']
    if 'integerValue' in x: return int(x['integerValue'])
    if 'doubleValue' in x: return x['doubleValue']
    if 'timestampValue' in x: return x['timestampValue']
    if 'nullValue' in x: return None
    if 'mapValue' in x: return {k:firestore_unwrap(v) for k,v in x['mapValue'].get('fields',{}).items()}
    if 'arrayValue' in x: return [firestore_unwrap(v) for v in x['arrayValue'].get('values',[])]
    return None


def firestore_doc(name, access_token):
    url = (f'https://firestore.googleapis.com/v1/projects/{PROJECT}'
           f'/databases/(default)/documents/settings/{urllib.parse.quote(name, safe="")}')
    try:
        r = api(url,token=access_token,timeout=20)
        return {k:firestore_unwrap(v) for k,v in r.get('fields',{}).items()}
    except Exception as e:
        if 'HTTP 404' not in str(e): print('    [Firestore]',name, str(e)[:100])
        return {}


def service_cfg():
    return get_json_from_cmd('gcloud','run','services','describe',SERVICE,'--project',PROJECT,'--region',REGION,'--format=json')


def env_map(service):
    container = service.get('spec',{}).get('template',{}).get('spec',{}).get('containers',[{}])[0]
    return {e.get('name'):e for e in container.get('env',[]) if e.get('name')}


def key_from_env(env,service,name):
    entry = env.get(name) or {}
    if entry.get('value'): return entry['value'], 'Cloud Run environment'
    ref = entry.get('valueFrom',{}).get('secretKeyRef',{})
    if not ref: return '', 'not found in Cloud Run environment'
    alias = ref.get('name','')
    version = ref.get('key') or 'latest'
    annotations = service.get('spec',{}).get('template',{}).get('metadata',{}).get('annotations',{})
    table = annotations.get('run.googleapis.com/secrets','')
    aliases = dict((v.split(':',1) for v in table.split(',') if ':' in v))
    target = aliases.get(alias,alias)
    secret = target.rsplit('/secrets/',1)[-1]
    try:
        result = run('gcloud','secrets','versions','access',str(version),'--secret',secret,'--project',PROJECT)
        return result, 'Secret Manager'
    except Exception as e:
        return '', 'secret referenced but inaccessible from Cloud Shell ('+str(e)[:80]+')'


def settings_details(docs):
    editor = {}
    for route in ('news','events'):
        raw = docs.get('editor' + ('_events' if route=='events' else ''),{})
        cfg = raw.get('value',{})
        if not isinstance(cfg,dict): cfg={}
        editor[route] = cfg
        fields = ['language','ai_order','review_provider','cost_mode','text_mode','image_mode','image_order',
                  'require_image','image_daily_limit','daily_limit','interval_minutes','body_length','gemini_model',
                  'openai_model','image_model','openai_image_model','free_mode','no_ai']
        print(f'  [{route}] Editor config:')
        for key in fields:
            if key in cfg: print(f'    {key}: {str(cfg[key])[:95]}')
        if not cfg: print('    default settings or no Firestore access')
        ledger = docs.get('image_ledger' + ('_events' if route=='events' else ''),{}).get('value')
        if not isinstance(ledger,dict): ledger=docs.get('image_ledger' + ('_events' if route=='events' else ''),{})
        if ledger: print('    image_ledger:', {k:ledger[k] for k in ('day','count') if k in ledger})
    return editor


def get_model(cfg, name, fallback):
    v = cfg.get(name)
    return v.strip() if isinstance(v,str) and v.strip() else fallback


def ukr_check(title,body):
    both=(title+' '+body).strip()
    chars=[c for c in both if c.isalpha()]
    cyr = sum('\u0400' <= c <= '\u052f' for c in chars)
    ratio = round(cyr / max(1,len(chars)),2)
    return {'cyrillic_ratio':ratio,'ukrainian_chars':any(c in both.lower() for c in 'іїєґ'),'ok':ratio>0.55 and len(body)>30}


def text_test(provider,key,model):
    prompt = ('You are a Ukrainian-language editor. Translate this Finnish event notice into natural Ukrainian. '
              'Preserve facts, do not invent information. Respond as JSON with non-empty title and body. '
              'Use Ukrainian, not Finnish and not Russian. Source: '+FINNISH_EXAMPLE)
    if provider == 'gemini':
        payload = {'contents':[{'parts':[{'text':prompt}]}],
                   'generationConfig':{'responseMimeType':'application/json','temperature':0.1,'maxOutputTokens':450}}
        out=api(f'https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model)}:generateContent',
                api_key=key, api_kind='gemini',payload=payload,timeout=55)
        parts=out['candidates'][0]['content']['parts']
        s=''.join(x.get('text','') for x in parts)
    else:
        payload={'model':model,'messages':[{'role':'user','content':prompt}],
                 'response_format':{'type':'json_object'}, 'max_completion_tokens':400}
        out=api('https://api.openai.com/v1/chat/completions',api_key=key,payload=payload,timeout=55)
        s=out['choices'][0]['message']['content']
    obj=json.loads(s.strip().removeprefix('```json').removesuffix('```'))
    title=str(obj.get('title',''));body=str(obj.get('body',''))
    check=ukr_check(title,body)
    return {**check,'preview_title':title[:110], 'preview_body':body[:170], 'model':model}


def image_test(provider,key,model,outdir):
    if provider=='gemini':
        payload={'contents':[{'parts':[{'text':IMAGE_PROMPT}]}],
                 'generationConfig':{'responseModalities':['TEXT','IMAGE']}}
        out=api(f'https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model)}:generateContent',
                api_key=key,api_kind='gemini',payload=payload,timeout=120)
        content=out.get('candidates',[{}])[0].get('content',{}).get('parts',[])
        imageparts=[p['inlineData'] for p in content if isinstance(p,dict) and p.get('inlineData',{}).get('data')]
        if not imageparts: raise RuntimeError('HTTP 200 but response had no image (possible text-only response / blocked output)')
        img=imageparts[0]
        extension='.png' if img.get('mimeType')=='image/png' else '.jpg'
        data=base64.b64decode(img['data'])
    else:
        payload={'model':model,'prompt':IMAGE_PROMPT,'n':1,'size':'1024x1024','quality':'low'}
        out=api('https://api.openai.com/v1/images/generations',api_key=key,payload=payload,timeout=150)
        data=base64.b64decode(out['data'][0]['b64_json']);extension='.png'
    if len(data)<3000: raise RuntimeError('Image data unusually small')
    path=outdir / ('test_'+provider+extension)
    path.write_bytes(data)
    return {'ok':True,'model':model,'bytes':len(data),'image':str(path)}


def recent_errors():
    flt=f'resource.type="cloud_run_revision" AND resource.labels.service_name="{SERVICE}" AND severity>=WARNING'
    try:
        entries=get_json_from_cmd('gcloud','logging','read',flt,'--project',PROJECT,'--limit=120','--format=json')
    except Exception as e:
        print('  Cloud Logging unavailable:',str(e)[:110]);return
    mapping = {
      'image_quota':r'(?i)(дневной лимит|image.daily.limit|limit.*image)',
      'image_api':r'(?i)(image.*(HTTPStatusError|429|400|403|404)|image generation .*failed)',
      'writer_api':r'(?i)(Writer .* failed|All configured AI writers unavailable|model.*not found)',
      'reviewer_api':r'(?i)(Reviewer .* unavailable)',
      'timeout':r'(?i)TimeoutError|timed out',
      'value_error':r'(?i)ValueError',
      'telegram':r'(?i)Telegram.*(error|failed)|sendPhoto.*(error|failed)',
    }
    counts=collections.Counter()
    for entry in entries:
        t=str(entry.get('textPayload') or entry.get('jsonPayload',{}).get('message') or '')
        for name,pat in mapping.items():
            if re.search(pat,t): counts[name]+=1
    print('  Recent Cloud Run WARNING+ categories (last 120 entries):',dict(counts))
    print('  Note: 0 warnings does not guarantee success; API errors may be stored in Firestore.')


def main():
    p=argparse.ArgumentParser(description='Test configured AI translators and image generators without sending Telegram messages')
    p.add_argument('--test-images',action='store_true',help='Make at most 1 image API request per provider; may incur a charge')
    p.add_argument('--skip-logs',action='store_true')
    args=p.parse_args()
    ts=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')
    audit_root=Path.home()/'bot-ai-audits';audit_root.mkdir(exist_ok=True)
    outdir=audit_root/('ai-test-'+ts);outdir.mkdir(exist_ok=False)
    report={'tested_at_utc':ts,'project':PROJECT,'service':SERVICE,'translation':{},'image':{}}
    print('=== Finland Bot AI audit / NO Telegram publishing / NO changes ===')
    print('UTC:', dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'))
    print('Cloud project:', PROJECT, '/ service:',SERVICE)
    try:
        service=service_cfg()
    except Exception as e:
        print('STOP: Cannot read Cloud Run metadata:',str(e)[:160]);return 2
    traffic=service.get('status',{}).get('traffic',[])
    print('Current Cloud Run traffic:', [{k:i.get(k) for k in ('revisionName','latestRevision','percent') if k in i} for i in traffic])
    server_url=service.get('status',{}).get('url')
    if server_url:
        try:
            health=api(server_url.rstrip('/')+'/health',timeout=18)
            print('Live /health:', {k:v for k,v in health.items() if k in ('ok','version','features')})
            report['health']={k:v for k,v in health.items() if k in ('ok','version','features')}
        except Exception as e:
            print('Live /health unavailable:',str(e)[:120])
    env=env_map(service)
    print('Runtime APP_VERSION:', (env.get('APP_VERSION') or {}).get('value','(not declared)'))
    print('Runtime UA_TRANSLATION_MODE:',(env.get('UA_TRANSLATION_MODE') or {}).get('value','(not declared)'))
    docs={}
    print('\n[1] Read actual editor / image limits from Firestore (read only)')
    try:
        token=run('gcloud','auth','print-access-token')
        for name in ['editor','editor_events','image_ledger','image_ledger_events',
                     'last_collect','last_collect_events','publication_ledger','publication_ledger_events']:
            docs[name]=firestore_doc(name,token)
    except Exception as e:
        print('  Firestore introspection not available:',str(e)[:140])
    editor=settings_details(docs)
    print('\n[2] Resolve configured API credentials (never display actual keys)')
    keys={}
    for provider in ('gemini','openai'):
        key,source=key_from_env(env,service,provider.upper()+'_API_KEY')
        # Runtime-added keys from the bot's /ai menu may be stored separately.
        keys[provider]=key
        print(' ',provider,'configured_in_cloud_run=',bool(key), 'source=',source)
    print('\n[3] Real API test: translate Finnish to Ukrainian')
    report['config']={name:{k:v for k,v in cfg.items() if k in ('language','ai_order','image_mode','image_order','image_daily_limit','require_image','review_provider','gemini_model','openai_model','image_model','openai_image_model')} for name,cfg in editor.items()}
    for provider in ('gemini','openai'):
        cfg=editor['events']
        model=get_model(cfg,provider+'_model',DEFAULTS[provider+'_text'])
        if provider == 'gemini' and not cfg.get('gemini_model') and (env.get('GEMINI_MODEL') or {}).get('value'):
            model=env['GEMINI_MODEL']['value']
        if not keys[provider]:
            report['translation'][provider]={'ok':False,'status':'SKIP','reason':'No readable Cloud Run key; runtime key may reside in Firestore'}
            print(' ',provider,'SKIP: key not accessible in this Cloud Shell session');continue
        try:
            data=text_test(provider,keys[provider],model)
            report['translation'][provider]=data
            print(' ',provider,'PASS' if data['ok'] else 'FAIL Ukrainian validation', 'model=',model,
                  'cyrillic=',data['cyrillic_ratio'],'title=',data['preview_title'])
        except Exception as e:
            report['translation'][provider]={'ok':False,'model':model,'error':str(e)[:200]}
            print(' ',provider,'FAIL',model,str(e)[:200])
    print('\n[4] Image generation')
    if not args.test_images:
        print('  SKIP: add --test-images for one real paid-or-quota-limited image request per provider')
    else:
        for provider in ('gemini','openai'):
            cfg=editor['events']
            field='image_model' if provider=='gemini' else 'openai_image_model'
            model=get_model(cfg,field,DEFAULTS[provider+'_image'])
            if not keys[provider]:
                report['image'][provider]={'ok':False,'status':'SKIP','reason':'No readable key'}
                print(' ',provider,'SKIP: key not accessible');continue
            try:
                data=image_test(provider,keys[provider],model,outdir)
                report['image'][provider]=data
                print(' ',provider,'PASS image=',data['image'],'size=',data['bytes'],'bytes; model=',model)
            except Exception as e:
                report['image'][provider]={'ok':False,'model':model,'error':str(e)[:200]}
                print(' ',provider,'FAIL',model,str(e)[:200])
    print('\n[5] Why publication may be blocked')
    cfg=editor.get('events',{})
    if cfg.get('image_mode')=='generate': print('  WARNING: image_mode=generate ignores original source pictures and relies on paid AI.')
    if cfg.get('image_mode')=='off' and cfg.get('require_image'):print('  BLOCKER: image_mode=off while require_image=true')
    if cfg.get('review_provider') not in (None,'off',''): print('  CHECK: reviewer may block publication on API timeout even when draft is valid.')
    if cfg.get('language') and cfg.get('language')!='українська': print('  CHECK: events language not pure Ukrainian:',cfg['language'])
    ledger=docs.get('image_ledger_events',{}).get('value') or docs.get('image_ledger_events',{})
    if isinstance(ledger,dict) and ledger.get('count') is not None and cfg.get('image_daily_limit') is not None:
        if int(ledger['count'])>=int(cfg['image_daily_limit']): print('  BLOCKER: image attempts exhausted for recorded ledger day',ledger.get('day'))
    print('  NOTE: language setting controls prompts, but code must validate final output; do not auto-publish an untranslated source.')
    print('\n[6] Recent errors')
    if not args.skip_logs:recent_errors()
    report_path=outdir/'result.json'
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print('\n=== DONE ===')
    print('Report:',report_path)
    print('Read-only for Firestore/Cloud Run/Telegram; external AI test calls may use API quota or incur charges.')
    print('Share terminal output (do NOT paste your API keys).')
    return 0

if __name__=='__main__':
    sys.exit(main())
