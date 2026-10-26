"""Human-facing admin workflows: input dialogs, browsing, budgets and backups."""
import asyncio
import hashlib
import json
import secrets
from datetime import timedelta
from zoneinfo import ZoneInfo
from . import channels,policy,sources,media,usage

def module(bot): return bot

async def dialog(bot,chat,user,kind,key=''):
    if kind in ('channel','restore') and not await bot.key_access(user):return await bot.say(chat,'Это действие доступно владельцу основного канала.')
    prompts={'setting':'Введите новое значение для '+key+'. /cancel — отмена.',
        'channel':'Пришлите: @канал | news или events или mixed | Название. Бот и вы должны иметь права публикации.',
        'title':'Пришлите новый заголовок (до 300 символов). /cancel — отмена.',
        'search':'Введите слово или фразу для поиска по заголовкам и текстам выбранного канала.',
        'source':'Пришлите: Название | https://адрес | rss или linked или manual. Сайт без RSS/API добавляйте как manual, затем импортируйте конкретные анонсы.',
        'photo':'Пришлите новую фотографию одним сообщением. /cancel — отмена.',
        'logo':'Пришлите логотип как фотографию или PNG/JPEG-документ до 2 МБ. Он будет использован в шаблонах этого канала.',
        'schedule':'Введите дату и время Helsinki: YYYY-MM-DD HH:MM. После этого подтвердите планирование.',
        'sourcefrequency':'Введите интервал проверки источника в минутах (5–1440).'}
    await bot.put_setting(f'dialog:{user}',{'kind':kind,'key':key,'route':bot.ROUTE.get(),'expires':bot.now()+timedelta(minutes=10)})
    return await bot.say(chat,prompts[kind])

async def consume(bot,chat,user,message,text):
    state=await bot.setting(f'dialog:{user}')
    if not isinstance(state,dict): return False
    await bot.put_setting(f'dialog:{user}',None)
    if state.get('expires',bot.now())<=bot.now():
        await bot.say(chat,'Время ввода истекло. Откройте нужное действие снова.');return True
    if not await bot.select_context(chat,user,state['route']):return True
    kind,key=state['kind'],state.get('key','')
    try:
        if kind=='setting':
            await bot.change_config(key,text)
            section=next((k for k,(_,keys) in bot.SECTIONS.items() if key in keys),None)
            await bot.show_settings(chat,'ai_options' if section=='ai' else section)
        elif kind=='title':
            if not await bot.authorize_item(user,key):raise ValueError('Нет доступа к материалу.')
            title=bot.sanitize(text,300)
            if not title:raise ValueError('Заголовок не может быть пустым.')
            if not await bot.transition_item(bot.db.collection('items').document(key),{'title':title,'title_fi':'','status':'pending','sensitive':True,'review_passed':False},tuple(s for s in bot.publication.MANUAL_STATUSES if s!='published')):raise ValueError('Материал уже отправлен или обрабатывается.')
            await bot.item_status(chat,key)
        elif kind=='channel': await channels.add(bot,chat,user,text)
        elif kind=='search':
            await bot.put_setting(f'search:{user}',text[:100]);await browse(bot,chat,user)
        elif kind=='source':
            parts=[p.strip() for p in text.split('|')]
            if len(parts)!=3 or not bot.valid_url(parts[1]) or parts[2] not in ('rss','linked','manual'):raise ValueError('Нужны название, публичный HTTPS-адрес и тип rss/linked/manual через |.')
            sid=hashlib.sha256((bot.ROUTE.get()+parts[1]).encode()).hexdigest()[:20]
            cfg=await bot.editor_config();value={'name':parts[0][:80],'url':parts[1],'kind':parts[2],'route':bot.ROUTE.get(),
                'category':'events' if bot.channel_kind(bot.ROUTE.get())=='events' else 'news','enabled':False,'images_allowed':False,'check_minutes':15}
            if parts[2]!='manual':
                rows,_=await sources.fetch(value,cfg,bot.now());value.update(last_count=len(rows),last_ok=bot.now(),last_error='',enabled=True)
            await asyncio.to_thread(bot.db.collection('sources').document(sid).set,value)
            await bot.say(chat,'Источник проверен и сохранён. Изображения источника по умолчанию выключены до подтверждения прав.');await bot.show_sources(chat)
        elif kind=='sourcefrequency':
            if not text.isdigit() or not 5<=int(text)<=1440:raise ValueError('Введите целое число 5–1440.')
            if not await bot.authorize_source(user,key):raise ValueError('Нет доступа к источнику.')
            await asyncio.to_thread(bot.db.collection('sources').document(key).update,{'check_minutes':int(text)})
            await bot.show_sources(chat)
        elif kind in ('photo','logo'):
            photos=message.get('photo',[]);doc=message.get('document',{})
            file_id=photos[-1]['file_id'] if photos else doc.get('file_id')
            if not file_id:raise ValueError('Нужна фотография или PNG/JPEG-файл.')
            if kind=='photo':
                if not await bot.authorize_item(user,key):raise ValueError('Нет доступа к материалу.')
                if not await bot.transition_item(bot.db.collection('items').document(key),
                    {'image_file_id':file_id,'image_kind':'source','image_bytes':bot.firestore.DELETE_FIELD,'image_error':''},
                    tuple(s for s in bot.publication.MANUAL_STATUSES if s!='published')):raise ValueError('Материал уже отправлен или обрабатывается.')
                await bot.item_status(chat,key)
            else:
                if doc and (doc.get('mime_type') not in ('image/png','image/jpeg') or doc.get('file_size',0)>2_000_000):raise ValueError('Нужен PNG/JPEG до 2 МБ.')
                info=await bot.tg('getFile',{'file_id':file_id})
                path=info.get('file_path','')
                if not path or '..' in path or path.startswith('/'):raise ValueError('Не удалось получить изображение.')
                response=await bot.app.state.telegram_client.get('https://api.telegram.org/file/bot'+bot.BOT_TOKEN+'/'+path,timeout=15)
                response.raise_for_status()
                if len(response.content)>2_000_000:raise ValueError('Файл больше 2 МБ.')
                normalized=await asyncio.to_thread(media.jpeg,response.content)
                await bot.put_setting(bot.route_key('logo'),normalized)
                await bot.say(chat,'Логотип сохранён. Он появится в новых изображениях-шаблонах.')
        elif kind=='schedule':
            at=sources.date(text.replace(' ','T'))
            if not at or at<=bot.now() or at>bot.now()+timedelta(days=180):raise ValueError('Укажите время в будущем, не более чем через 180 дней.')
            pending=await bot.setting(f'manual_send:{user}')
            if not isinstance(pending,dict) or pending.get('nonce')!=key or pending.get('expires',bot.now())<=bot.now():raise ValueError('Сначала выберите материал и канал; превью истекло.')
            pending['scheduled_at']=at
            await bot.put_setting(f'manual_send:{user}',pending)
            await bot.say(chat,'Запланировать показанный пост на '+at.astimezone(ZoneInfo('Europe/Helsinki')).strftime('%d.%m.%Y %H:%M')+' в '+bot.target_channel(pending['target'])+'?',[
                [{'text':'Подтвердить планирование','callback_data':'sendconfirm:'+key}],[{'text':'Отмена','callback_data':'sendcancel'}]])
    except Exception as error:
        await bot.say(chat,str(error)[:350] if isinstance(error,ValueError) else 'Не удалось выполнить действие: '+type(error).__name__+'. Проверьте данные и повторите.')
    return True

async def browse(bot,chat,user,field=None,value=None,page=0):
    key=f'browse:{user}:{bot.ROUTE.get()}';filters=await bot.setting(key,{})
    if field=='reset':filters={};await bot.put_setting(f'search:{user}','')
    elif field:filters[field]=value
    await bot.put_setting(key,filters)
    search=(await bot.setting(f'search:{user}','')).casefold()
    current=bot.now().astimezone(ZoneInfo('Europe/Helsinki')).date()
    docs=[]
    for doc in await bot.item_metadata():
        item=doc.to_dict()
        if bot.route_of(item)!=bot.ROUTE.get():continue
        if search and search not in (item.get('title','')+' '+item.get('text','')+' '+item.get('summary','')).casefold():continue
        event=item.get('event',{});start=event.get('start');day=start.astimezone(ZoneInfo('Europe/Helsinki')).date() if start else None
        if filters.get('city') not in (None,'all') and event.get('city','').casefold()!=filters['city'].casefold():continue
        period=filters.get('period','all')
        if period!='all' and (day is None or not (current+timedelta(days=1 if period=='tomorrow' else 0)<=day<=current+timedelta(days={'today':0,'tomorrow':1,'week':7,'month':30}[period]))):continue
        if filters.get('price')=='free' and event.get('is_free') is not True:continue
        if filters.get('price')=='paid' and event.get('is_free') is not False:continue
        if filters.get('audience')=='uk' and not ('uk' in event.get('languages',[]) or 'ukrain' in item.get('source_id','') or 'україн' in item.get('title','').casefold()):continue
        if filters.get('category') not in (None,'all') and item.get('category')!=filters['category']:continue
        docs.append(doc)
    docs.sort(key=lambda d:d.to_dict().get('created_at',bot.now()),reverse=True)
    page=max(0,min(page,max(0,(len(docs)-1)//4)))
    for doc in docs[page*4:page*4+4]:await bot.item_status(chat,doc.id)
    rows=[[{'text':'←','callback_data':f'browsepage:{max(0,page-1)}'},{'text':'→','callback_data':f'browsepage:{page+1}'}],
          [{'text':'Поиск','callback_data':'dialog:search'},{'text':'Сбросить фильтры','callback_data':'browse:reset:all'}]]
    if bot.channel_kind(bot.ROUTE.get())!='news':
        rows += [[{'text':name,'callback_data':'browse:period:'+value} for value,name in (('today','Сегодня'),('tomorrow','Завтра'),('week','Неделя'),('month','Месяц'))],
                 [{'text':city,'callback_data':'browse:city:'+city} for city in ('Helsinki','Espoo','Vantaa')],
                 [{'text':'Бесплатные','callback_data':'browse:price:free'},{'text':'Платные','callback_data':'browse:price:paid'}],
                 [{'text':'Для украинцев','callback_data':'browse:audience:uk'},{'text':'Все жители','callback_data':'browse:audience:all'}]]
    rows += [[{'text':name,'callback_data':'browse:category:'+category}] for category,name in policy.CATEGORY_NAMES.items()]
    rows += [[{'text':'Назад','callback_data':'home'}]]
    return await bot.say(chat,f'Найдено: {len(docs)} • страница {page+1}\nФильтры: {filters}\nПоиск: {search or "—"}',rows)

async def usage_report(bot,chat):
    route=bot.ROUTE.get();cfg=await bot.editor_config()
    ledger=(await asyncio.to_thread(bot.db.collection('ai_usage').document(route+'-'+bot.now().strftime('%Y-%m')).get)).to_dict() or {}
    requests=[d.to_dict() for d in await bot.documents(bot.db.collection('ai_requests').where('route','==',route))]
    requests=[r for r in requests if r.get('at') and r['at'].strftime('%Y-%m')==bot.now().strftime('%Y-%m')]
    known=[r['cost'] for r in requests if r.get('cost') is not None]
    items=[d.to_dict() for d in await bot.item_metadata() if bot.route_of(d.to_dict())==route]
    lines=['📊 ИИ и экономия • '+bot.target_channel(route),'Режим: '+cfg['cost_mode'],
           f'Попыток API за месяц: {ledger.get("attempts",0)} / {cfg["ai_monthly_limit"]}',
           f'Дневной предел попыток: {cfg["ai_daily_limit"]}',
           f'Без ИИ: {sum(i.get("text_method")=="original" for i in items)}; из кеша: {sum(i.get("text_method")=="cache" for i in items)}',
           f'Локальных карточек: {sum(i.get("image_kind")=="template" for i in items)}',
           f'Стоимость, сообщённая API: {sum(known):.6f} USD ({len(known)} запросов).',
           f'Стоимость остальных {len(requests)-len(known)} запросов неизвестна. Это не ноль и не счёт Google Cloud.',
           'Лимиты считаются по попыткам, включая ошибки. Бесплатный режим не переключается на платные модели.']
    return await bot.panel(chat,'\n'.join(lines),[[{'text':'Без ИИ: оригинал + шаблон','callback_data':'mode:off'}],
        [{'text':'Максимально бесплатно','callback_data':'mode:free'}],[{'text':'Режим и лимиты','callback_data':'section:budget'},{'text':'Назад','callback_data':'ai'}]])

async def backup(bot,chat,user,restore=False):
    if not await bot.key_access(user):return await bot.say(chat,'Резервные копии настроек доступны владельцу основного канала.')
    if not restore:
        data={}
        for collection in ('settings','sources','channels'):
            values=[]
            for doc in await bot.documents(bot.db.collection(collection)):
                if collection=='settings' and not (doc.id.startswith(('editor','autopublish','notifications'))):continue
                values.append({'id':doc.id,'value':doc.to_dict()})
            data[collection]=values
        identity=bot.now().strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)
        # Store each record separately: no Firestore document grows with the number of sources.
        await asyncio.to_thread(bot.db.collection('config_backups').document(identity).set,{'created_at':bot.now(),'created_by':user,'status':'writing'})
        for collection,rows in data.items():
            for row in rows:
                await asyncio.to_thread(bot.db.collection('config_backup_records').document(identity+'-'+collection+'-'+row['id']).set,
                    {'backup':identity,'collection':collection,**row})
        await asyncio.to_thread(bot.db.collection('config_backups').document(identity).update,{'status':'ready'})
        return await bot.say(chat,'Копия настроек создана: '+identity+'. Ключи, история и материалы не менялись.',[
            [{'text':'Восстановить эту копию','callback_data':'restore:'+identity}]])

async def restore_backup(bot,chat,user,identity,confirmed=False):
    if not await bot.key_access(user):return await bot.say(chat,'Нет доступа.')
    snap=await asyncio.to_thread(bot.db.collection('config_backups').document(identity).get)
    if not snap.exists or snap.to_dict().get('status')!='ready':return await bot.say(chat,'Готовая копия не найдена.')
    if not confirmed:
        return await bot.say(chat,'Восстановить настройки, источники и каналы из '+identity+'? История и API-ключи сохраняются. Новые записи не удаляются.',[
            [{'text':'Подтверждаю восстановление','callback_data':'restoreok:'+identity}],[{'text':'Отмена','callback_data':'home'}]])
    for doc in await bot.documents(bot.db.collection('config_backup_records').where('backup','==',identity)):
        value=doc.to_dict()
        if value['collection'] not in ('settings','sources','channels'):continue
        await asyncio.to_thread(bot.db.collection(value['collection']).document(value['id']).set,value['value'])
    await channels.load(bot)
    return await bot.say(chat,'Настройки восстановлены. Откройте меню и проверьте состояние автопубликации.')
