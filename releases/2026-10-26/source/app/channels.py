"""Persistent channel registry. Archival never deletes publication history."""
import asyncio
import hashlib
import re

async def load(bot):
    entries={'news':{'telegram_id':bot.CHANNEL_ID,'type':'news','name':'Новости','enabled':True},
             'events':{'telegram_id':bot.EVENTS_CHANNEL_ID,'type':'events','name':'События','enabled':True}}
    for doc in await bot.documents(bot.db.collection('channels')):
        value=doc.to_dict()
        if re.fullmatch(r'ch[a-f0-9]{12}',doc.id) and value.get('type') in ('news','events','mixed'):
            entries[doc.id]=value
    bot.CHANNELS.clear();bot.CHANNELS.update(entries)
    bot.controls.NAMES.clear();bot.controls.NAMES.update({k:v.get('name',k) for k,v in entries.items()})

async def show(bot,chat,user):
    rows=[]
    for route,value in bot.CHANNELS.items():
        if not await bot.admin_access(user,route):continue
        if value.get('enabled',True):
            rows.append([{'text':('✅ ' if route==bot.ROUTE.get() else '')+value.get('name',route)+' · '+value['telegram_id'],
                          'callback_data':'route:'+route}])
        if route not in ('news','events'):
            rows.append([{'text':'Архивировать '+value['telegram_id'] if value.get('enabled',True) else 'Восстановить '+value['telegram_id'],
                          'callback_data':('chremove:' if value.get('enabled',True) else 'chrestore:')+route}])
    rows += [[{'text':'➕ Добавить канал','callback_data':'dialog:channel'}],[{'text':'Назад','callback_data':'home'}]]
    return await bot.panel(chat,'📢 Каналы. Настройки и история каждого канала независимы. Добавленные каналы собираются по очереди вместе с основными заданиями Scheduler.',rows)

async def add(bot,chat,user,text):
    if not await bot.key_access(user):return await bot.say(chat,'Каналы добавляет владелец основного канала.')
    parts=[v.strip() for v in text.split('|')]
    if len(parts)!=3 or not re.fullmatch(r'@[A-Za-z0-9_]{5,32}|-100\d{5,}',parts[0]) or parts[1] not in ('news','events','mixed'):
        return await bot.say(chat,'Формат: @имя_канала | news или events или mixed | Название. Сначала добавьте бота администратором с правом публикации.')
    target,kind,name=parts
    me=await bot.tg('getMe',{})
    for actor in (me['id'],user):
        member=await bot.tg('getChatMember',{'chat_id':target,'user_id':actor})
        if member.get('status')!='creator' and not (member.get('status')=='administrator' and member.get('can_post_messages')):
            return await bot.say(chat,'У вас и бота должны быть права публикации в новом канале.')
    if any(v['telegram_id'].casefold()==target.casefold() for v in bot.CHANNELS.values()):
        return await bot.say(chat,'Этот канал уже добавлен.')
    route='ch'+hashlib.sha256(target.casefold().encode()).hexdigest()[:12]
    await asyncio.to_thread(bot.db.collection('channels').document(route).set,
        {'telegram_id':target,'type':kind,'name':name[:45] or target,'enabled':True,'created_by':user,'created_at':bot.now()})
    for sid,source in bot.CATALOG.items():
        if kind!='mixed' and source.get('route')!=kind: continue
        copied=route+'-'+hashlib.sha256(sid.encode()).hexdigest()[:8]
        await asyncio.to_thread(bot.db.collection('sources').document(copied).set,dict(source,route=route))
    await bot.put_setting(bot.route_key('autopublish',route),False)
    await load(bot)
    await bot.select_context(chat,user,route)
    return await show(bot,chat,user)
