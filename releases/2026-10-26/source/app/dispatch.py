"""Short publication-only worker; shares the collector lease and never prepares AI text."""
import asyncio
import secrets
from datetime import timedelta

async def run(bot,route):
    if not bot.CHANNELS.get(route,{}).get('enabled',True):return {'disabled':True}
    lease=bot.db.collection('settings').document(bot.route_key('collector_lease',route))
    token=secrets.token_hex(12)
    @bot.firestore.transactional
    def acquire(tx):
        value=lease.get(transaction=tx).to_dict() or {}
        if value.get('until',bot.now())>bot.now():return False
        tx.set(lease,{'token':token,'until':bot.now()+timedelta(seconds=290)})
        return True
    @bot.firestore.transactional
    def release(tx):
        if (lease.get(transaction=tx).to_dict() or {}).get('token')==token:tx.delete(lease)
    if not await asyncio.to_thread(acquire,bot.db.transaction()):return {'busy':True}
    context=bot.ROUTE.set(route)
    try:
        published=await asyncio.wait_for(bot.jobs.run_due(bot,route),timeout=240)
        await bot.put_setting(bot.route_key('last_dispatch',route),{'at':bot.now(),'published':published})
        return {'published':published}
    finally:
        bot.ROUTE.reset(context)
        await asyncio.to_thread(release,bot.db.transaction())

async def endpoint(bot):
    # Only channels with outstanding jobs are touched. No feed polling and no inference.
    docs=await bot.documents(bot.db.collection('scheduled_posts').where('status','==','scheduled'))
    running=await bot.documents(bot.db.collection('scheduled_posts').where('status','==','running'))
    routes=sorted({d.to_dict()['route'] for d in docs+running})
    result={}
    for offset in range(0,len(routes),3):
        group=routes[offset:offset+3]
        values=await asyncio.gather(*(run(bot,r) for r in group),return_exceptions=True)
        result.update({r:v if isinstance(v,dict) else {'error':type(v).__name__} for r,v in zip(group,values)})
    return result
