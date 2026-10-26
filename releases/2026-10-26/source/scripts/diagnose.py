"""Read-only deployment checks. Never print credentials or full environment values."""
import argparse,json,os,re,subprocess,sys,urllib.request
from datetime import datetime,timezone

PROJECT=os.getenv('PROJECT_ID','winged-ratio-377512')
REGION=os.getenv('REGION','europe-north1')
LOCATION=os.getenv('SCHEDULER_LOCATION','europe-west1')
SERVICE='finland-news-bot'

def gcloud(*args):
    r=subprocess.run(['gcloud',*args,'--project='+PROJECT,'--format=json'],capture_output=True,text=True)
    if r.returncode: raise RuntimeError('gcloud '+args[0]+' '+args[1]+' failed (check permissions/configuration)')
    return json.loads(r.stdout or 'null')

def secret(name):
    r=subprocess.run(['gcloud','secrets','versions','access','latest','--secret='+name,'--project='+PROJECT],capture_output=True,text=True)
    if r.returncode: raise RuntimeError('Cannot read secret '+name)
    return r.stdout.strip()

def request(url,body=None,headers=None):
    payload=json.dumps(body).encode() if body is not None else None
    req=urllib.request.Request(url,payload,dict({'Content-Type':'application/json'},**(headers or {})))
    return json.load(urllib.request.urlopen(req,timeout=45))

def service(): return gcloud('run','services','describe',SERVICE,'--region='+REGION)

def preflight(expected_version=None):
    obj=service();status=obj['status'];url=status['url']
    ready=next((c.get('status') for c in status.get('conditions',[]) if c['type']=='Ready'),None)
    print('Cloud Run:',ready,'revision:',status.get('latestReadyRevisionName'),'URL:',url)
    if ready!='True': raise RuntimeError('Cloud Run is not Ready')
    health=request(url+'/health')
    print('Health:',health)
    print('Traffic:',status.get('traffic',[]))
    if health.get('ok') is not True: raise RuntimeError('Health check failed')
    if expected_version and health.get('version')!=expected_version:
        raise RuntimeError('Traffic does not serve expected application version '+expected_version)
    env=obj['spec']['template']['spec']['containers'][0].get('env',[])
    values={e['name']:e.get('value') for e in env}
    print('Legacy AI environment bindings:',[e['name'] for e in env if e['name'].endswith('_API_KEY') and e.get('valueFrom')])
    if 'runtime_ai_keys' in health.get('features',[]):
        print('Runtime AI connections:',request(url+'/admin/ai-status',{}, {'X-Cron-Secret':secret('cron-secret')}))
    if values.get('ADMIN_AUTH_MODE')!='channel_admins' and values.get('ADMIN_ID','0')=='0':
        print('NOTICE: administrator access is currently disabled (ADMIN_ID=0). Upgrade enables channel_admins.')
    token=secret('bot-token');base='https://api.telegram.org/bot'+token
    bot=request(base+'/getMe')['result']
    print('Telegram: @'+bot['username'])
    channels=(values.get('CHANNEL_ID') or '@suominewsua',os.getenv('EVENTS_CHANNEL_ID') or values.get('EVENTS_CHANNEL_ID') or '@eventhsl')
    for channel in channels:
        member=request(base+'/getChatMember',{'chat_id':channel,'user_id':bot['id']})['result']
        can_post=member.get('status')=='creator' or member.get('status')=='administrator' and member.get('can_post_messages') is True
        print(channel,'can_post_messages:',can_post)
        if not can_post: raise RuntimeError('Give the bot posting rights in '+channel)
    hook=request(base+'/getWebhookInfo')['result']
    print('Webhook:',hook.get('url'),'pending:',hook.get('pending_update_count'))
    if hook.get('last_error_date'):
        print('Last webhook error:',datetime.fromtimestamp(hook['last_error_date'],timezone.utc).isoformat(),hook.get('last_error_message'))
    if hook.get('url')!=url+'/telegram': raise RuntimeError('Webhook does not point to this service')
    return obj

def diagnose():
    failures=0
    for label,fn in [
        ('Billing',lambda:gcloud('billing','projects','describe',PROJECT)),
        ('Service and Telegram',preflight),
        ('Recent build',lambda:gcloud('builds','list','--region='+REGION,'--limit=1','--sort-by=~createTime')),
    ]:
        try:
            value=fn()
            if label=='Billing': print(label,{k:value.get(k) for k in ('projectId','billingEnabled','billingAccountName')})
            elif label=='Recent build': print(label,[{k:b.get(k) for k in ('id','status','createTime','logUrl')} for b in value])
        except Exception as error: print(label,type(error).__name__,str(error) if isinstance(error,RuntimeError) else 'request failed'); failures+=1
    try:
        url=service()['status']['url']
        if 'publication_diagnostics' in request(url+'/health').get('features',[]):
            report=request(url+'/admin/publication-status',{}, {'X-Cron-Secret':secret('cron-secret')})
            print('Publication diagnostics:',json.dumps(report,ensure_ascii=False,indent=2))
        else: print('Publication diagnostics: update required; use the bot settings/status in the current version')
    except Exception as error:
        print('Publication diagnostics failed:',type(error).__name__);failures+=1
    for job in ('finland-news-collect','finland-events-collect','finland-news-watchdog','finland-news-dispatch'):
        try:
            value=gcloud('scheduler','jobs','describe',job,'--location='+LOCATION)
            print('Scheduler',job,{k:value.get(k) for k in ('state','schedule','timeZone','lastAttemptTime','status')})
        except Exception: print('Scheduler',job,'not available (events job appears after upgrade)')
    logs=subprocess.run(['gcloud','run','services','logs','read',SERVICE,'--project='+PROJECT,'--region='+REGION,'--freshness=2h','--limit=35'],capture_output=True,text=True)
    print('Recent logs:')
    text=logs.stdout
    for name in ('bot-token','gemini-api-key','openai-api-key','groq-api-key','deepseek-api-key','mistral-api-key','openrouter-api-key'):
        try:
            value=secret(name)
            if value: text=text.replace(value,'[REDACTED]')
        except Exception: pass
    text=re.sub(r'bot\d+:[\w-]+','bot[REDACTED]',text)
    print(text if text else 'No logs returned')
    return failures

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--preflight',action='store_true');parser.add_argument('--migrate',action='store_true');parser.add_argument('--expected-version');parser.add_argument('--candidate');args=parser.parse_args()
    try:
        if args.candidate:
            health=request(args.candidate+'/health')
            if health.get('ok') is not True or health.get('version')!=args.expected_version:raise RuntimeError('Candidate version/health mismatch')
            check=request(args.candidate+'/admin/self-test',{}, {'X-Cron-Secret':secret('cron-secret')})
            if check.get('ok') is not True:raise RuntimeError('Candidate template test failed')
            print('Candidate verified without AI calls or Telegram publications:',check)
        elif args.preflight: preflight(args.expected_version)
        elif args.migrate:
            url=service()['status']['url']
            print('Migration:',request(url+'/admin/migrate',{}, {'X-Cron-Secret':secret('cron-secret')}))
        else: sys.exit(1 if diagnose() else 0)
    except Exception as error:
        print('Check failed:',str(error) if isinstance(error,RuntimeError) else type(error).__name__)
        sys.exit(1)
