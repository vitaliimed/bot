"""Redacted persistent errors and deduplicated admin notifications."""
import asyncio
import hashlib
from datetime import timedelta

async def error(bot,component,route,code,*,item_id='',source_id='',recommendation='',notify=False):
    identity=hashlib.sha256((component+'|'+route+'|'+str(code)+'|'+item_id+'|'+source_id).encode()).hexdigest()[:32]
    ref=bot.db.collection('errors').document(identity)
    @bot.firestore.transactional
    def update(tx):
        old=ref.get(transaction=tx).to_dict() or {}
        tx.set(ref,{'component':component,'route':route,'code':str(code)[:80],'item_id':item_id,'source_id':source_id,
            'first_at':old.get('first_at',bot.now()),'last_at':bot.now(),'attempts':old.get('attempts',0)+1,
            'recommendation':recommendation,'status':'open'})
    await asyncio.to_thread(update,bot.db.transaction())
    if notify:await alert(bot,route,component+':'+str(code),f'⚠️ {component}\n{bot.target_channel(route)}\nКод: {code}\n{recommendation}\nОткройте «Диагностика» или /failed.')

async def alert(bot,route,key,text):
    cfg=await bot.editor_config(route)
    if not cfg.get('notifications',True):return
    ref=bot.db.collection('notification_locks').document(hashlib.sha256((route+key).encode()).hexdigest()[:32])
    @bot.firestore.transactional
    def claim(tx):
        old=ref.get(transaction=tx).to_dict() or {}
        if old.get('until',bot.now())>bot.now():return False
        tx.set(ref,{'until':bot.now()+timedelta(hours=1)});return True
    if not await asyncio.to_thread(claim,bot.db.transaction()):return
    for doc in await bot.documents(bot.db.collection('admin_chats')):
        value=doc.to_dict();user=value.get('user')
        if not isinstance(user,int) or not await bot.admin_access(user) or not await bot.admin_access(user,route):continue
        try:await bot.tg('sendMessage',{'chat_id':user,'text':bot.fit(text,3500)})
        except Exception:pass

async def show(bot,chat):
    rows=[d for d in await bot.documents(bot.db.collection('errors')) if d.to_dict().get('route')==bot.ROUTE.get()]
    rows.sort(key=lambda d:d.to_dict().get('last_at',bot.now()),reverse=True)
    for doc in rows[:8]:
        value=doc.to_dict();buttons=[]
        if value.get('item_id'):buttons.append([{'text':'Открыть материал','callback_data':'status:'+value['item_id']}])
        buttons.append([{'text':'Отметить исправленной','callback_data':'errorresolved:'+doc.id}])
        await bot.say(chat,f'{value.get("status")} • {value["component"]} • {value["code"]}\n'
            f'{value["last_at"]} • попыток: {value["attempts"]}\nИсточник: {value.get("source_id") or "—"}\n{value["recommendation"]}',buttons)
    if not rows:await bot.say(chat,'Зарегистрированных ошибок пока нет.')
