"""Explicit maintenance operations, source assignment and persistent health warnings."""
import asyncio
import hashlib
from datetime import datetime,timedelta,timezone

async def menu(bot,chat):
    return await bot.panel(chat,'🛠 Обслуживание выбранного канала. Изменения очереди требуют подтверждения.',[
        [{'text':'Собрать сейчас','callback_data':'collect'},{'text':'Обработать очередь','callback_data':'collect'}],
        [{'text':'Повторить ошибки','callback_data':'op:retry'},{'text':'Выбрать готовые','callback_data':'op:ready'}],
        [{'text':'Очистить очередь в архив','callback_data':'op:archive'}],
        [{'text':'Проверить источники','callback_data':'sources'},{'text':'Проверить Telegram','callback_data':'checktelegram'}],
        [{'text':'Проверить ИИ','callback_data':'ai'},{'text':'Назад','callback_data':'home'}]])

async def prepare(bot,chat,user,action):
    statuses={'retry':('awaiting_ai',),'ready':('pending',),'archive':('pending','queued','awaiting_ai','filtered','expired')}
    if action not in statuses:return
    docs=[d for d in await bot.item_metadata() if bot.route_of(d.to_dict())==bot.ROUTE.get() and d.to_dict().get('status') in statuses[action]]
    ids=[d.id for d in docs[:10]]
    await bot.put_setting(f'selection:{user}',ids)
    if action=='ready':return await bot.jobs.show(bot,chat,user)
    return await bot.jobs.prepare_bulk(bot,chat,user,'retry' if action=='retry' else 'reject','_')

async def assign(bot,chat,user,sid,target=None):
    if not await bot.authorize_source(user,sid):return await bot.say(chat,'Нет доступа к источнику.')
    ref=bot.db.collection('sources').document(sid);source=(await asyncio.to_thread(ref.get)).to_dict()
    if target is None:
        rows=[]
        for route,value in bot.CHANNELS.items():
            if value.get('enabled',True) and await bot.admin_access(user,route):
                # Store source in a short per-user state to keep callbacks within 64 bytes.
                rows.append([{'text':bot.target_channel(route),'callback_data':'sourcecopy:'+route}])
        await bot.put_setting(f'sourcecopy:{user}',{'id':sid,'expires':bot.now()+timedelta(minutes=5)})
        return await bot.panel(chat,'Добавить этот источник в другой канал с независимыми настройками? Исходный источник сохранится.',rows)
    if target not in bot.CHANNELS or not await bot.admin_access(user,target):return await bot.say(chat,'Нет доступа к каналу.')
    identity=hashlib.sha256((target+'|'+source['url']).encode()).hexdigest()[:20]
    new=bot.db.collection('sources').document(identity)
    if not (await asyncio.to_thread(new.get)).exists:
        copied={k:v for k,v in source.items() if k not in ('last_ok','last_error','last_count','next_page','found_count')}
        copied['route']=target
        await asyncio.to_thread(new.create,copied)
    return await bot.say(chat,'Источник добавлен в '+bot.target_channel(target)+'. История исходного канала сохранена.')

async def watchdog(bot):
    """Called by an independent endpoint; it does not collect or publish articles."""
    result={}
    for route,value in bot.CHANNELS.items():
        if not value.get('enabled',True):continue
        warnings=[]
        last=await bot.setting(bot.route_key('last_collect',route),{})
        try:at=datetime.fromisoformat(last.get('at','').replace('Z','+00:00'))
        except (ValueError,TypeError):at=None
        if last.get('error'):warnings.append('Последний сбор завершился ошибкой: '+str(last['error'])[:80])
        if not at or (bot.now()-at).total_seconds()>5400:warnings.append('Нет запуска сбора больше 90 минут')
        items=[d.to_dict() for d in await bot.item_metadata() if bot.route_of(d.to_dict())==route]
        pending=sum(i.get('status') in ('queued','awaiting_ai','pending') for i in items)
        if pending>200:warnings.append(f'Очередь растёт: {pending} материалов')
        if await bot.setting(bot.route_key('autopublish',route),False):
            published=[i.get('published_at') for i in items if isinstance(i.get('published_at'),datetime)]
            if pending and (not published or bot.now()-max(published)>timedelta(hours=24)):
                warnings.append('Есть очередь, но публикаций не было больше суток')
        cfg=await bot.editor_config(route)
        row=(await asyncio.to_thread(bot.db.collection('ai_usage').document(route+'-'+datetime.now(timezone.utc).strftime('%Y-%m-%d')).get)).to_dict() or {}
        if cfg['ai_daily_limit'] and row.get('attempts',0)>=cfg['ai_daily_limit']*.8:warnings.append('Использовано не менее 80% дневного лимита ИИ')
        if warnings:await bot.observability.alert(bot,route,'watchdog','⚠️ '+bot.target_channel(route)+'\n'+'\n'.join(warnings)+'\nОткройте диагностику.')
        result[route]=warnings
    try:
        hook=await bot.tg('getWebhookInfo',{})
        if hook.get('pending_update_count',0)>10:
            await bot.observability.alert(bot,'news','webhook-backlog','⚠️ Растёт очередь webhook Telegram. Проверьте состояние сервиса и логи.')
    except Exception:result['telegram']=['Telegram не ответил']
    return result


async def fastnews(bot,chat,user,confirmed=False):
    if bot.channel_kind(bot.ROUTE.get())=='events':return await bot.say(chat,'Этот профиль предназначен для новостей. Выберите новостной канал.')
    if not confirmed:
        await bot.put_setting(f'fastnews:{user}',{'route':bot.ROUTE.get(),'expires':bot.now()+timedelta(minutes=5)})
        return await bot.say(chat,'Включить быстрые новости без ИИ?\nАвтопубликация ВКЛ, до 60 постов в день, интервал 5 минут, подготовка до 20 за сбор. Текст останется на исходном языке, фото из источника или бесплатный шаблон. Модерация всех постов выключится, но рискованные материалы и повторы продолжат блокироваться. Темы, регионы и часы работы сохранятся. RSS этого канала будет проверяться не чаще чем раз в 5 минут; частота Scheduler задаётся при обновлении.',[[{'text':'Подтвердить быстрые новости без ИИ','callback_data':'fastnewsconfirm'}],[{'text':'Отмена','callback_data':'home'}]])
    state=await bot.setting(f'fastnews:{user}')
    if not isinstance(state,dict) or state.get('route')!=bot.ROUTE.get() or state.get('expires',bot.now())<=bot.now():return await bot.say(chat,'Подтверждение истекло.')
    await bot.put_setting(f'fastnews:{user}',None)
    await bot.put_setting(bot.route_key('before_fastnews'),{'editor':await bot.editor_config(),'auto':await bot.setting(bot.route_key('autopublish'),False)})
    changes={'cost_mode':'off','text_mode':'original','image_mode':'source_template','review_provider':'off','moderation':False,'daily_limit':60,'interval_minutes':5,'processing_batch_size':20}
    await asyncio.to_thread(bot.db.collection('settings').document(bot.route_key('editor')).set,{'value':changes},merge=True)
    for doc in await bot.documents(bot.db.collection('sources')):
        source=doc.to_dict()
        if bot.route_of(source)==bot.ROUTE.get() and source.get('kind','rss')=='rss':await asyncio.to_thread(doc.reference.update,{'check_minutes':5})
    await bot.put_setting(bot.route_key('autopublish'),True)
    return await bot.say(chat,'Профиль включён. Приоритетные слова задаются в Настройки → Темы и фильтры. Нажмите «Собрать» для запуска. История и фильтры сохранены; предыдущие настройки сохранены в before_fastnews.')
