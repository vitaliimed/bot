"""Persistent attempt quotas and explicit free/paid routing, shared across instances."""
import asyncio
import math
import secrets
from datetime import datetime,timezone
from google.cloud import firestore

database=None

class BudgetBlocked(ValueError): pass

def allowed(provider,model,cfg):
    if provider in cfg.get('disabled_ai',[]): return False
    mode=cfg.get('cost_mode','free')
    if mode=='off': return False
    if mode=='paid': return True
    # Explicit free model routing; never fall back to a paid OpenRouter model.
    if provider=='openrouter': return model=='openrouter/free' or model.endswith(':free')
    # Cloudflare is permitted only after the owner confirms Workers Free in its dashboard.
    return provider=='cloudflare' and cfg.get('cloudflare_free_account') is True

async def reserve(provider,model,cfg,task='text'):
    if not allowed(provider,model,cfg): raise BudgetBlocked('Провайдер/модель запрещены текущим режимом расходов')
    if database is None: return None
    current=datetime.now(timezone.utc);route=cfg.get('_route','news')
    refs=[database.collection('ai_usage').document(route+'-'+current.strftime(fmt)) for fmt in ('%Y-%m-%d','%Y-%m')]
    @firestore.transactional
    def claim(tx):
        values=[r.get(transaction=tx).to_dict() or {} for r in refs]
        for value,limit in zip(values,(cfg['ai_daily_limit'],cfg['ai_monthly_limit'])):
            if value.get('attempts',0)>=limit: raise BudgetBlocked('Лимит запросов ИИ исчерпан')
        for ref,value in zip(refs,values):
            tx.set(ref,dict(value,attempts=value.get('attempts',0)+1,updated_at=current))
    await asyncio.to_thread(claim,database.transaction())
    identity=secrets.token_hex(12)
    await asyncio.to_thread(database.collection('ai_requests').document(identity).set,
        {'at':current,'route':route,'provider':provider,'model':model,'task':task,'status':'started','cost':None})
    return identity

async def finish(identity,status,usage=None,code=None):
    if not database or not identity:return
    value={'status':status,'finished_at':datetime.now(timezone.utc)}
    if code is not None: value['code']=str(code)[:30]
    usage=usage or {}
    for key in ('prompt_tokens','completion_tokens','total_tokens'):
        if type(usage.get(key)) is int:value[key]=usage[key]
    if isinstance(usage.get('cost'),(int,float)) and math.isfinite(usage['cost']) and usage['cost']>=0:
        value.update(cost=float(usage['cost']),cost_source='provider',currency='USD')
    await asyncio.to_thread(database.collection('ai_requests').document(identity).update,value)
