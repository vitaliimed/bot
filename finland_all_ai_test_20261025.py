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



# ---------- ALL-PROVIDER EXTENSION (read only; never publishes) ----------
PROVIDER_KEYS = {
    'gemini': ('GEMINI_API_KEY',),
    'openai': ('OPENAI_API_KEY',),
    'deepseek': ('DEEPSEEK_API_KEY',),
    'cloudflare': ('CLOUDFLARE_API_TOKEN','CLOUDFLARE_API_KEY','CLOUDFLARE_WORKERS_AI_API_TOKEN'),
    'groq': ('GROQ_API_KEY',),
    'mistral': ('MISTRAL_API_KEY',),
    'openrouter': ('OPENROUTER_API_KEY',),
    'huggingface': ('HUGGINGFACE_API_KEY','HUGGINGFACE_TOKEN','HF_TOKEN'),
}
PROVIDER_MODELS = {
    'gemini':'gemini-3.5-flash-lite',
    'openai':'gpt-4.1-mini',
    'deepseek':'deepseek-chat',
    'cloudflare':'@cf/meta/llama-3.1-8b-instruct',
    'groq':'llama-3.3-70b-versatile',
    'mistral':'mistral-small-latest',
    'openrouter':'openai/gpt-4.1-mini',
    'huggingface':'Qwen/Qwen2.5-7B-Instruct',
}
TEXT_URLS = {
    'openai':'https://api.openai.com/v1/chat/completions',
    'deepseek':'https://api.deepseek.com/chat/completions',
    'groq':'https://api.groq.com/openai/v1/chat/completions',
    'mistral':'https://api.mistral.ai/v1/chat/completions',
    'openrouter':'https://openrouter.ai/api/v1/chat/completions',
    'huggingface':'https://router.huggingface.co/v1/chat/completions',
}
SOURCE_NOTICE = ('Helsingissä järjestetään maksuton perhetapahtuma lauantaina '
                 '31. lokakuuta klo 12. Tapahtumapaikka on keskustakirjasto Oodi.')
PROMPT = ('Переклади фінський текст українською, зберігши всі факти, і відповідай '
          'лише українською без пояснень, не додавай жодних нових фактів: ' + SOURCE_NOTICE)
ALLOWED_FIRESTORE_SETTINGS = (
    'ai_keys','runtime_ai_keys','ai_providers','provider_keys','ai_config',
    'ai_settings','ai_api_keys','runtime_ai','integrations','ai_runtime',
)

def gcloud_keys(env, service, p):
    # Explicit environment/Secret Manager references only. Values never printed.
    for name in PROVIDER_KEYS[p]:
        k,source=key_from_env(env,service,name)
        if k: return k,source
        if os.getenv(name): return os.environ[name],'Cloud Shell environment'
    return '', 'not exposed in Cloud Run environment'


def get_settings_any(name, token):
    return firestore_doc(name,token)


def firestore_list(collection_name, token, limit=80):
    url=(f'https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)'
         f'/documents/{urllib.parse.quote(collection_name,safe="")}?' +
         urllib.parse.urlencode({'pageSize':min(limit,100)}))
    try:
        result=api(url,token=token,timeout=20)
        return [(doc.get('name','').split('/')[-1],
                 {k:firestore_unwrap(v) for k,v in doc.get('fields',{}).items()})
                for doc in result.get('documents',[])]
    except Exception:
        return []


def scan_possible_ai_settings(token):
    # Only read likely AI settings; do not print, persist or expose credential values.
    docs={}
    for k in ALLOWED_FIRESTORE_SETTINGS:
        val=get_settings_any(k,token)
        if val: docs[k]=val
    for doc_id, data in firestore_list('settings',token):
        if doc_id not in docs and re.search(r'(ai|provider|credential|token|key)',doc_id,re.I):
            docs[doc_id]=data
    for coll in ('ai_keys','ai_providers','ai_credentials','provider_keys'):
        for id_,d in firestore_list(coll,token):
            if id_.lower() in PROVIDER_KEYS:
                docs[coll+'/'+id_]=d
    return docs


def flatten(x, prefix='', depth=0):
    if depth>7:return
    if isinstance(x,dict):
        for k,v in x.items():
            yield from flatten(v,(prefix+'.'+str(k)).strip('.'),depth+1)
    elif isinstance(x,list):
        for i,v in enumerate(x[:30]):
            yield from flatten(v,prefix+'.'+str(i),depth+1)
    else:
        yield prefix,x


def resolve_secret_pointer(value):
    # Use only the explicit references provided by this bot's own configuration.
    value=str(value)
    m=re.fullmatch(r'projects/[^/]+/secrets/([^/]+)(?:/versions/([^/]+))?',value)
    if m: secret,version=m.group(1),m.group(2) or 'latest'
    else:
        # Do not guess a secret ID from a generic string.
        return ''
    try:
        return run('gcloud','secrets','versions','access',version,
                   '--secret',secret,'--project',PROJECT)
    except Exception:
        return ''


def firestore_key_guess(provider, docs):
    # Key fields must match a provider-specific path; never interpret arbitrary
    # text such as a model ID or a Firestore doc value as an API credential.
    key_tokens=('api_key','api_token','token','credential','secret_ref','secret_name',
                'secret_id','secret','key')
    for doc_name, doc in docs.items():
        for path,value in flatten(doc):
            full=(doc_name+'.'+path).lower().replace('-','_')
            if provider not in full:continue
            if not isinstance(value,str) or not value.strip():continue
            if not any(t in full for t in key_tokens):continue
            if '/secrets/' in value:
                k=resolve_secret_pointer(value)
                if k:return k,'Firestore Secret Manager reference'
            # Candidate plain API key: length/prefix check avoids mistaking a model
            # name, help text, status flag, or account ID for a credential.
            v=value.strip()
            has_key_field=any(re.search(r'(?:^|[._/])'+re.escape(t)+r'$',full)
                              for t in key_tokens)
            if has_key_field and len(v)>=24 and re.fullmatch(r'[A-Za-z0-9_./+=-]+',v):
                if v.startswith(('sk-','AIza','hf_','gsk_','sk-or-','sk-proj-')) or len(v)>38:
                    return v,'Firestore runtime key'
    return '', 'not accessible to audit script'


def cloudflare_account(env,docs):
    names=('CLOUDFLARE_ACCOUNT_ID','CF_ACCOUNT_ID','WORKERS_AI_ACCOUNT_ID')
    for name in names:
        entry=env.get(name,{})
        if entry.get('value'): return entry['value']
        if os.getenv(name):return os.getenv(name)
    for _,data in docs.items():
        for path,v in flatten(data):
            if ('cloudflare' in path.lower() or 'account_id' in path.lower()) and \
               re.search(r'(cloudflare[_./-]?account[_./-]?id|cf[_./-]?account[_./-]?id)',path,re.I) \
               and isinstance(v,str) and re.fullmatch('[a-fA-F0-9]{32}',v):
                return v
    return ''


def safe_api_json(url,key,payload,kind,timeout=50):
    headers={'Content-Type':'application/json','Accept':'application/json'}
    if kind=='gemini':headers['x-goog-api-key']=key
    else:headers['Authorization']='Bearer '+key
    request=urllib.request.Request(url,headers=headers,
                data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),method='POST')
    try:
        with urllib.request.urlopen(request,timeout=timeout) as response:
            b=response.read(1000000)
            ct=response.headers.get('content-type','')
            if 'image/' in ct:return {'_binary_image':b}
            try:return json.loads(b)
            except ValueError:raise RuntimeError('HTTP 200 non-JSON response')
    except urllib.error.HTTPError as error:
        try:
            obj=json.loads(error.read(5000))
            err=obj.get('error',obj) if isinstance(obj,dict) else {}
            if isinstance(err,dict):
                code=str(err.get('code') or err.get('type') or '')
                msg=str(err.get('message') or '')
            else:code,msg='',str(err)
        except Exception:code,msg='', ''
        # Filter any account/token-like or suspiciously long strings returned by upstream.
        msg=re.sub(r'(sk-[A-Za-z0-9_-]+|AIza[A-Za-z0-9_-]+|hf_[A-Za-z0-9_-]+)',
                   '[REDACTED]',msg)
        msg=re.sub(r'\b[A-Za-z0-9_\-]{48,}\b','[REDACTED]',msg)
        raise RuntimeError('HTTP '+str(error.code)+' '+code+' '+msg[:95].replace('\n',' ')) from None
    except (TimeoutError,urllib.error.URLError) as error:
        raise RuntimeError('NETWORK '+type(error).__name__) from None


def get_response(provider,key,model,cf_account=''):
    if provider=='gemini':
        obj=safe_api_json('https://generativelanguage.googleapis.com/v1beta/models/'+
                          urllib.parse.quote(model,safe='')+':generateContent',key,
                          {'contents':[{'parts':[{'text':PROMPT}]}],
                           'generationConfig':{'temperature':0.1,'maxOutputTokens':250}},
                          'gemini',timeout=55)
        return ''.join(x.get('text','') for x in
                       obj['candidates'][0]['content']['parts'] if x.get('text'))
    if provider=='cloudflare':
        if not cf_account:raise RuntimeError('SKIP Cloudflare account ID unavailable')
        obj=safe_api_json('https://api.cloudflare.com/client/v4/accounts/'+
                          urllib.parse.quote(cf_account,safe='')+'/ai/run/'+model,
                          key,{'prompt':PROMPT,'max_tokens':250},'cloudflare',timeout=55)
        if not obj.get('success',True):
            errors=obj.get('errors',[])
            raise RuntimeError('HTTP 200 provider rejected request; errors='+str(
                               [e.get('code') for e in errors if isinstance(e,dict)])[:100])
        result=obj.get('result',{})
        return str(result.get('response') or result.get('text') or '')
    opts={'model':model,'messages':[{'role':'user','content':PROMPT}],
          'temperature':0.1}
    if provider=='openai':opts['max_completion_tokens']=250
    else:opts['max_tokens']=250
    obj=safe_api_json(TEXT_URLS[provider],key,opts,provider,timeout=55)
    return obj['choices'][0]['message']['content'] or ''


def evaluate_ukrainian(s):
    value=re.sub(r'<[^>]+>','',str(s))
    letters=[c for c in value if c.isalpha()]
    cyr=sum('\u0400'<=c<='\u052F' for c in letters)
    ratio=round(cyr/max(1,len(letters)),2)
    has_ua=any(ch in value.lower() for ch in 'іїєґ')
    fact=('31' in value and ('12' in value or 'дванадц' in value))
    return {'pass':ratio>=.65 and has_ua and fact,'cyrillic':ratio,
            'ua_letters':has_ua,'facts_preserved':fact,
            'title_preview':value[:85]}


def cf_image_test(token,account,outdir):
    model='@cf/black-forest-labs/flux-1-schnell'
    prompt='Clean flat editorial illustration of Helsinki tram in autumn, no text, no logos'
    obj=safe_api_json('https://api.cloudflare.com/client/v4/accounts/'+account+
                      '/ai/run/'+model, token, {'prompt':prompt,'steps':4},
                      'cloudflare',timeout=80)
    if '_binary_image' in obj:data=obj['_binary_image']
    else:
        encoded=(obj.get('result',{}) or {}).get('image','')
        if not encoded:raise RuntimeError('HTTP 200 but no image data')
        data=base64.b64decode(encoded,validate=True)
    if len(data)<1024:raise RuntimeError('Image too small')
    kind='png' if data[:8]==b'\x89PNG\r\n\x1a\n' else 'jpg'
    target=outdir/('cloudflare_flux_test.'+kind)
    target.write_bytes(data)
    return {'ok':True,'bytes':len(data),'model':model,'path':str(target)}


def service_code_inspection(base):
    # Read-only: returns code symbols without printing source or any secret values.
    tokens=['text_mode','cost_mode','ai_order','translate','image_mode',
            'source_template','image_error','free_mode','ai_status']
    matches={k:[] for k in tokens}
    app=base/'app'
    if not app.exists():return matches
    for f in app.rglob('*.py'):
        if '.git' in f.parts:continue
        try:content=f.read_text(encoding='utf-8')
        except Exception:continue
        for name in tokens:
            n=len(re.findall(r'\b'+re.escape(name)+r'\b',content,re.I))
            if n:matches[name].append(f'{f.relative_to(base)} ({n})')
    return matches


def main():
    parser=argparse.ArgumentParser(description='Read-only tests of all 8 AI providers for Finland News Bot')
    parser.add_argument('--images',choices=('none','cloudflare'),default='none',
                        help='Cloudflare FLUX image test may use credits; default no image requests')
    parser.add_argument('--retry-openai',action='store_true',help='Retry OpenAI despite confirmed no credits')
    parser.add_argument('--retry-gemini',action='store_true',help='Repeat successful Gemini translation test')
    args=parser.parse_args()
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')
    outdir=Path.home()/'bot-ai-audits'/('all-ai-'+stamp)
    outdir.mkdir(parents=True,exist_ok=True)
    report={'date_utc':stamp,'project':PROJECT,'translation':{},'settings':{},'images':{}}
    print('=== ALL 8 AI PROVIDERS / BOTH CHANNELS / NO CHANGES / NO TELEGRAM POSTING ===')
    try: service=service_cfg()
    except Exception as exc:
        print('STOP Cannot read Cloud Run:',str(exc)[:125]);return 2
    traffic=[(t.get('revisionName'),t.get('percent')) for t in service.get('status',{}).get('traffic',[])]
    env=env_map(service)
    version=(env.get('APP_VERSION') or {}).get('value','unknown')
    print('Live traffic:',traffic,'runtime version:',version)
    print('READ ONLY: No publication, no deployment, no settings writes, no Git actions.')
    docs={}
    try:
        access=run('gcloud','auth','print-access-token')
        for name in ('editor','editor_events','image_ledger','image_ledger_events',
                     'publication_ledger','publication_ledger_events'):
            docs[name]=firestore_doc(name,access)
        runtime_docs=scan_possible_ai_settings(access)
    except Exception as exc:
        print('Firestore read access unavailable:',type(exc).__name__)
        runtime_docs={}
    editors={}
    for route in ('news','events'):
        stored=docs.get('editor'+('_events' if route=='events' else ''),{}).get('value',{})
        cfg=stored if isinstance(stored,dict) else {}
        editors[route]=cfg
        # Intentionally omit sensitive fields; only whitelist operational settings.
        keys=('language','text_mode','cost_mode','ai_order','image_mode','image_order',
              'review_provider','require_image','image_daily_limit','body_length',
              'gemini_model','openai_model','deepseek_model','cloudflare_model',
              'groq_model','mistral_model','openrouter_model','huggingface_model')
        visible={k:cfg[k] for k in keys if k in cfg}
        report['settings'][route]=visible
        print(f'\n[{route}]',json.dumps(visible,ensure_ascii=False))
        ledger=docs.get('image_ledger'+('_events' if route=='events' else ''),{})
        if isinstance(ledger.get('value'),dict):ledger=ledger['value']
        print('  image attempts:',{k:ledger.get(k) for k in ('day','count') if k in ledger})
        mode=cfg.get('image_mode')
        if mode in ('source','source_template'):
            print('  CAUSE: image mode',mode,'does not force AI image generation.')
        if cfg.get('cost_mode')=='free':
            print('  CAUSE TO CHECK: FREE mode may reject selected paid-only AI model.')
    print('\n[1] Resolve provider credentials (never display actual keys)')
    keys={};sources={}
    for provider in PROVIDER_KEYS:
        key,src=gcloud_keys(env,service,provider)
        if not key:key,src=firestore_key_guess(provider,runtime_docs)
        keys[provider]=key;sources[provider]=src
        print(f'  {provider:12s} key_accessible={bool(key)!s:5s} origin={src}')
    account=cloudflare_account(env,runtime_docs)
    print('  cloudflare account ID available:',bool(account))
    if not keys['cloudflare'] or not account:
        print('  NOTE: Telegram may display a stored runtime key inaccessible to this shell;')
        print('        SKIP does not mean the provider is disconnected in the bot.')
    print('\n[2] Real translation tests: each available provider/model once')
    cache={}
    for route in ('news','events'):
        cfg=editors[route]
        requested=cfg.get('ai_order') or list(PROVIDER_KEYS)
        print('  CHANNEL',route)
        for provider in PROVIDER_KEYS:
            if provider not in requested:
                print('   ',provider,'OFF: not in ai_order')
                continue
            model=cfg.get(provider+'_model') or PROVIDER_MODELS[provider]
            if provider=='openai' and not args.retry_openai:
                result={'status':'KNOWN_BLOCK','model':model,
                        'reason':'Previous test returned HTTP 429 insufficient_quota'}
            elif provider=='gemini' and not args.retry_gemini:
                # Unlike other providers, Gemini is already proven for this text model.
                if model=='gemini-3.5-flash-lite':
                    result={'status':'PREVIOUS_PASS','model':model,'reason':'Confirmed with real API on 2026-10-25'}
                else:
                    result={'status':'NOT_TESTED','model':model,'reason':'Configured model differs from successful previous test; add --retry-gemini'}
            elif not keys.get(provider):
                result={'status':'SKIP_KEY_UNREADABLE','model':model}
            elif provider=='cloudflare' and not account:
                result={'status':'SKIP_ACCOUNT_ID','model':model}
            elif (provider,model) in cache:
                result={**cache[(provider,model)],'reused':True}
            else:
                try:
                    s=get_response(provider,keys[provider],model,account)
                    verified=evaluate_ukrainian(s)
                    result={'status':'PASS' if verified['pass'] else 'LANGUAGE_OR_FACTS_FAIL',
                            'model':model, **verified}
                except Exception as exc:
                    result={'status':'API_FAIL','model':model,'error':str(exc)[:130]}
                cache[(provider,model)]=result
            report['translation'].setdefault(route,{})[provider]=result
            print('   ',provider,model,':',result['status'],
                  ('| '+str(result.get('error') or result.get('reason') or ''))[:140])
    print('\n[3] Image capability (no repeat of known Gemini / OpenAI 429)')
    if args.images=='cloudflare':
        if keys.get('cloudflare') and account:
            try:
                result=cf_image_test(keys['cloudflare'],account,outdir)
                print('  Cloudflare FLUX:',result['path'],result['bytes'],'bytes')
                report['images']['cloudflare']=result
            except Exception as exc:
                print('  Cloudflare FLUX: FAIL',str(exc)[:150])
                report['images']['cloudflare']={'status':'FAIL','error':str(exc)[:150]}
        else:
            print('  Cloudflare FLUX: SKIP (token or account ID inaccessible)')
    else: print('  SKIP: image API requests disabled; use --images cloudflare to test FLUX once')
    print('  Gemini image: PREVIOUS HTTP 429 (not retried)')
    print('  OpenAI image: PREVIOUS insufficient_quota (not retried)')
    print('\n[4] Local source inspection / read-only')
    code=service_code_inspection(Path.cwd())
    for symbol,files in code.items():
        if files:print('  ',symbol,':',', '.join(files[:5]))
    if not any(code.values()):print('  Local app source not found.')
    print('  NOTE: Cloud Shell local code might not match the deployed Cloud Build source.')
    path=outdir/'result.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('\n=== FINISHED ===')
    print('Report:',path)
    print('Never paste secret values. Share printed statuses and errors only.')
    return 0

if __name__=='__main__':
    sys.exit(main())
