"""Finland News Bot: Telegram webhook + scheduled RSS collector, Firestore storage."""
import os, re, json, html, hashlib, logging, secrets, asyncio, time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
import feedparser
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, HTTPException
from google.cloud import firestore
from .access import can_manage
from . import ai as ai_router, media, policy, sources, formatting

logging.basicConfig(level=logging.INFO)
logging.getLogger('httpx').setLevel(logging.WARNING)
log = logging.getLogger('newsbot')

@asynccontextmanager
async def lifespan(application):
    telegram_limits = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30)
    gemini_limits = httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=30)
    async with httpx.AsyncClient(timeout=25, limits=telegram_limits) as telegram_client, \
               httpx.AsyncClient(timeout=50, limits=gemini_limits) as gemini_client:
        application.state.telegram_client = telegram_client
        application.state.gemini_client = gemini_client
        yield

app = FastAPI(title='Finland News Bot', lifespan=lifespan)
BOT_TOKEN = os.environ['BOT_TOKEN']
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
ADMIN_AUTH_MODE = os.getenv('ADMIN_AUTH_MODE', 'id')
CHANNEL_ID = os.environ['CHANNEL_ID']
EVENTS_CHANNEL_ID = os.getenv('EVENTS_CHANNEL_ID', '@eventhsl')
ROUTE = ContextVar('route', default='news')
VERSION = os.getenv('APP_VERSION', 'two-channel-editor-v2')
WEBHOOK_SECRET = os.environ['WEBHOOK_SECRET']
CRON_SECRET = os.environ['CRON_SECRET']
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite')
db = firestore.Client()
TG = f'https://api.telegram.org/bot{BOT_TOKEN}'
CATALOG = json.loads(Path(__file__).with_name('catalog.json').read_text())

def route_of(item):
    return item.get('route') or ('events' if item.get('category') == 'events' else 'news')

def route_key(key, route=None):
    return key + ('_events' if (route or ROUTE.get()) == 'events' else '')

def target_channel(route):
    return EVENTS_CHANNEL_ID if route == 'events' else CHANNEL_ID

def channel_tag(route, cfg):
    # The channel identity cannot be removed by switching off topic hashtags.
    channel = target_channel(route)
    return '#' + (channel[1:] if channel.startswith('@') else cfg.get('channel_tag','').lstrip('#') or ('eventhsl' if route == 'events' else 'suominewsua'))

async def selected_route(chat):
    value = await setting(f'route:{chat}', 'news')
    return value if value in ('news','events') else 'news'

async def seed_catalog():
    added = 0
    for sid, value in CATALOG.items():
        ref = db.collection('sources').document(sid)
        if not (await asyncio.to_thread(ref.get)).exists:
            try:
                await asyncio.to_thread(ref.create,value)
                added += 1
            except Exception as error:
                if type(error).__name__ != 'AlreadyExists': raise
    return added

CATEGORIES = {'news':'#НовиниФінляндії', 'ukraine':'#УкраїнціУФінляндії', 'sport':'#Спорт', 'children':'#Дітям', 'events':'#Події', 'transport':'#Транспорт', 'other':'#Фінляндія'}

def now(): return datetime.now(timezone.utc)
async def documents(query):
    return await asyncio.to_thread(lambda: list(query.stream()))

async def setting(key, default=None):
    d = await asyncio.to_thread(db.collection('settings').document(key).get)
    return d.to_dict().get('value', default) if d.exists else default

async def put_setting(key, value):
    await asyncio.to_thread(db.collection('settings').document(key).set, {'value':value})

async def editor_config(route=None):
    route = route or ROUTE.get()
    stored = await setting(route_key('editor',route), {})
    if not isinstance(stored,dict): stored = {}
    cfg = policy.config(stored,route)
    cfg['_route'] = route
    if 'gemini_model' not in stored: cfg['gemini_model'] = GEMINI_MODEL
    return cfg

async def change_config(key, value):
    value = policy.validate(key, value)
    # Field updates preserve simultaneous changes by different administrators.
    await asyncio.to_thread(db.collection('settings').document(route_key('editor')).set,
                            {'value': {key: value}}, merge=True)
    return value

async def transition_item(ref, changes, allowed=('pending',)):
    @firestore.transactional
    def update(tx):
        snapshot = ref.get(transaction=tx)
        if not snapshot.exists or snapshot.to_dict().get('status') not in allowed: return False
        tx.update(ref,changes)
        return True
    return await asyncio.to_thread(update,db.transaction())

def sanitize(text, limit=4000):
    return BeautifulSoup(text or '', 'html.parser').get_text(' ', strip=True)[:limit]

def valid_url(url):
    if not isinstance(url,str) or len(url)>2000 or any(c.isspace() for c in url): return False
    try:
        p = urlparse(url)
        return p.scheme == 'https' and bool(p.hostname) and not (p.username or p.password) and p.port in (None,443) and p.hostname not in ('localhost', '127.0.0.1', '0.0.0.0')
    except ValueError: return False

async def tg(method, payload, files=None):
    for attempt in range(2):
        if files:
            data = {key: json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
                    for key, value in payload.items()}
            r = await app.state.telegram_client.post(f'{TG}/{method}', data=data, files=files)
        else:
            r = await app.state.telegram_client.post(f'{TG}/{method}', json=payload)
        if r.status_code == 429 and attempt == 0:
            retry_after = r.json().get('parameters', {}).get('retry_after', 1)
            if type(retry_after) is int and 0 <= retry_after <= 30:
                log.warning('Telegram rate limit for %s; retrying after %ss', method, retry_after)
                await asyncio.sleep(max(1, retry_after))
                continue
        r.raise_for_status()
        d = r.json()
        if not d.get('ok'): raise RuntimeError(str(d))
        return d['result']

async def say(chat, text, keyboard=None, parse_mode=None):
    payload = {'chat_id':chat, 'text':fit(text,4096), 'disable_web_page_preview':True}
    if parse_mode: payload.update(text=text,parse_mode=parse_mode)
    if keyboard: payload['reply_markup'] = {'inline_keyboard':keyboard}
    return await tg('sendMessage', payload)

async def admin_access(user_id, route=None):
    return await can_manage(user_id, mode=ADMIN_AUTH_MODE, admin_id=ADMIN_ID,
                            channel_id=target_channel(route or 'news'), telegram=tg)

def access_hint():
    if ADMIN_AUTH_MODE == 'channel_admins':
        return f'Управление доступно владельцу и администраторам канала {CHANNEL_ID} с правом публикации.'
    return 'Управление этим ботом доступно только назначенному администратору.'

def menu():
    return [[{'text':'📰 Новости','callback_data':'route:news'}, {'text':'📅 События','callback_data':'route:events'}],
            [{'text':'📝 Очередь','callback_data':'queue'}, {'text':'📡 Источники','callback_data':'sources'}],
            [{'text':'⚙️ Настройки','callback_data':'settings'}, {'text':'🤖 ИИ','callback_data':'section:ai'}],
            [{'text':'📊 Статистика','callback_data':'stats'}, {'text':'ℹ️ Помощь','callback_data':'help'}]]

async def dashboard(chat):
    auto, cfg = await asyncio.gather(setting(route_key('autopublish'),False),editor_config())
    await say(chat, f'🇫🇮 Finland Bot — {target_channel(ROUTE.get())}\n'
              f'Автопубликация: {"ВКЛ" if auto else "ВЫКЛ"}\nЯзык: {cfg["language"]}\n'
              f'Лимит: {cfg["daily_limit"]}/день • интервал {cfg["interval_minutes"]} мин.\n'
              'Выберите канал кнопками и откройте нужный раздел.', menu())

SECTIONS = {
    'schedule': ('🕒 Расписание', ['daily_limit','interval_minutes','window_start','window_end','timezone','weekdays','batch_size']),
    'topics': ('🎯 Темы и фильтры', ['categories','include_words','exclude_words','max_age_hours','event_cities','event_horizon_days','free_only','event_languages']),
    'text': ('✍️ Текст', ['language','tone','body_length','signature','hashtags','source_link','silent','channel_tag']),
    'images': ('🖼 Фото', ['image_mode','require_image','image_daily_limit','image_order','openai_image_model','image_model','image_quality']),
    'ai': ('🤖 ИИ и проверка', ['ai_order','ai_timeout','review_provider']+[p+'_model' for p in policy.PROVIDERS]),
}

async def show_settings(chat, section=None):
    cfg, auto = await asyncio.gather(editor_config(),setting(route_key('autopublish'),False))
    title = f'⚙️ {target_channel(ROUTE.get())} • обязательная подпись {channel_tag(ROUTE.get(),cfg)}'
    keyboard = [[{'text':'Автопубликация: '+('ВКЛ' if auto else 'ВЫКЛ'),'callback_data':'toggle'}]]
    if section not in SECTIONS:
        keyboard += [[{'text':label,'callback_data':'section:'+key}] for key,(label,_) in SECTIONS.items()]
        keyboard += [[{'text':'◀️ Каналы','callback_data':'home'}]]
        return await say(chat,title+'\nНастройки относятся только к выбранному каналу.\nРучная публикация обходит расписание и лимит постов.',keyboard)
    label, keys = SECTIONS[section]
    lines = [title,label]+[f'{k} = {",".join(map(str,cfg[k])) if isinstance(cfg[k],list) else cfg[k]}' for k in keys]
    if section == 'ai':
        lines += ['\nКлючи подключены (доступ к модели проверяется при запросе):']
        lines += [f'{p}: {"да" if configured else "нет"}; пауза {ai_router.cooldown(p,cfg[p+"_model"])} сек.' for p,configured in ai_router.available().items()]
        lines += ['Ключи вводятся в Cloud Shell: bash scripts/connect-ai.sh all']
    if section == 'topics':
        keyboard += [[{'text':('✅ ' if key in cfg['categories'] else '⬜ ')+name,'callback_data':'category:'+key}] for key,name in policy.CATEGORY_NAMES.items()]
    for key in keys:
        if key in policy.CHOICES or type(policy.DEFAULTS[key]) is bool:
            keyboard.append([{'text':'Изменить '+key,'callback_data':'cycle:'+key}])
        elif key in policy.RANGES:
            keyboard.append([{'text':'− '+key,'callback_data':'adjust:'+key+':-1'}, {'text':'+ '+key,'callback_data':'adjust:'+key+':1'}])
    keyboard.append([{'text':'◀️ Разделы','callback_data':'settings'}])
    lines += ['\n/set ИМЯ значение — точное значение; /reset ИМЯ — сброс.',
              'Например: /set daily_limit 12 или /set ai_order openai,gemini,groq,deepseek,mistral,openrouter']
    return await say(chat,'\n'.join(lines),keyboard)

async def show_sources(chat, page=0):
    docs = sorted([d for d in await documents(db.collection('sources')) if route_of(d.to_dict()) == ROUTE.get()],key=lambda d:d.id)
    page = max(0,min(page,max(0,(len(docs)-1)//8)))
    rows = docs[page*8:(page+1)*8]
    lines = [f'📡 {target_channel(ROUTE.get())}: {len(docs)} источников • страница {page+1}']
    keyboard = []
    for d in rows:
        v = d.to_dict()
        lines.append(f'{"🟢" if v.get("enabled",True) else "🔴"} {d.id}: {v.get("name", "")}\n{v.get("last_error") or "Последний сбор: "+str(v.get("last_ok","ещё не было"))}')
        keyboard.append([{'text':('Отключить ' if v.get('enabled',True) else 'Включить ')+d.id,'callback_data':'source:'+d.id}])
    keyboard.append([{'text':'←','callback_data':'sources:'+str(max(0,page-1))},{'text':'→','callback_data':'sources:'+str(page+1)}])
    lines += ['\n/addsource Название | https://rss-url | news (или events)', '/sourceimages ID on/off — разрешить фото источника.']
    return await say(chat,'\n'.join(lines),keyboard)

async def item_docs(status, route=None, limit=200):
    route = route or ROUTE.get()
    docs = await documents(db.collection('items').where('status','==',status))
    return sorted([d for d in docs if route_of(d.to_dict()) == route],key=lambda d:d.to_dict().get('created_at',now()))[:limit]

async def show_queue(chat, page=0):
    docs = await item_docs('pending',limit=1000)
    if not docs: return await say(chat,'Очередь пуста. /failed — ошибки; /collect — собрать материалы.')
    page = max(0,min(page,(len(docs)-1)//4))
    for d in docs[page*4:(page+1)*4]:
        v = d.to_dict()
        keyboard = [[{'text':'✅ Опубликовать','callback_data':'pub:'+d.id},{'text':'❌ Отклонить','callback_data':'reject:'+d.id}],
                    [{'text':'✏️ Текст','callback_data':'edit:'+d.id},{'text':'🖼 Превью','callback_data':'preview:'+d.id}]]
        event_info = policy.event_reason(v,now()) if route_of(v)=='events' else ''
        await say(chat,f'📝 {target_channel(route_of(v))} • ID {d.id}\n{v.get("title","")}\n{v.get("text","")[:650]}\n'
                  f'{v.get("title_fi","")}\n{v.get("text_fi","")[:350]}\n{v.get("url","")}\n'
                  f'Фото: {v.get("image_kind","нет")} {v.get("image_error","")}\nИИ: {v.get("ai_provider","—")}\n'
                  f'Проверка: {event_info or v.get("review_reason","")}',keyboard)
    return await say(chat,f'Страница {page+1}; всего {len(docs)}',[[{'text':'←','callback_data':'queue:'+str(max(0,page-1))},{'text':'→','callback_data':'queue:'+str(page+1)}]])

async def show_stats(chat):
    docs = await documents(db.collection('items'))
    counts = {}
    for d in docs:
        if route_of(d.to_dict()) != ROUTE.get(): continue
        status = d.to_dict().get('status','unknown')
        counts[status] = counts.get(status,0)+1
    run = await setting(route_key('last_collect'),{})
    return await say(chat,f'📊 {target_channel(ROUTE.get())}\n'+'\n'.join(f'{k}: {v}' for k,v in counts.items())+'\nПоследний сбор: '+str(run))

async def show_help(chat):
    return await say(chat,'/start — каналы и меню\n/channel news или /channel events — выбрать канал\n/settings — разделы настроек; /set ИМЯ значение\n'
        '/sources /queue /stats /failed /collect\n/auto_on /auto_off — выбранный канал\n/retry ID — повторить обработку\n'
        '/event ID | 2026-11-01T18:00 | Helsinki | Место и адрес | бесплатно или цена — проверенные данные события\n'
        '/ask вопрос — редакционный помощник ИИ (не меняет настройки и не публикует)\n'
        '/addsource Название | https://rss-url | events\n/enable ID /disable ID /deletesource ID\n'
        'Фото: источник → генерация. Если ИИ недоступен, нет даты или превышен лимит, материал остаётся для проверки.\n'
        'Для управления событиями нужны права публикации в обоих каналах.')

async def ai_prepare(title, summary, url, category, cfg=None):
    return await ai_router.prepare(app.state.gemini_client, title, summary, url, category,
                                   cfg or await editor_config(), CATEGORIES)

def fit(text, limit):
    # Telegram counts UTF-16 code units; emoji usually consume two.
    return text.encode('utf-16-le')[:limit * 2].decode('utf-16-le', errors='ignore')

def format_post(item, cfg=None, photo=False):
    cfg = cfg or policy.config(route=route_of(item))
    return formatting.render(item,cfg,channel_tag(route_of(item),cfg),photo)

async def send_item(chat, item, cfg):
    if cfg['image_mode'] != 'off' and (item.get('image_file_id') or item.get('image_bytes')):
        payload = {'chat_id':chat,'caption':format_post(item,cfg,photo=True),'parse_mode':'HTML','disable_notification':cfg['silent']}
        if item.get('image_file_id'):
            payload['photo'] = item['image_file_id']
            return await tg('sendPhoto',payload)
        return await tg('sendPhoto',payload,files={'photo':('news.jpg',item['image_bytes'],'image/jpeg')})
    if cfg['require_image']: raise ValueError('Image required')
    return await tg('sendMessage',{'chat_id':chat,'text':format_post(item,cfg),'parse_mode':'HTML',
                                  'disable_notification':cfg['silent'],'disable_web_page_preview':True})

async def publish(docid, automatic=False, cfg=None):
    ref = db.collection('items').document(docid)
    initial = await asyncio.to_thread(ref.get)
    if not initial.exists: return False
    route = route_of(initial.to_dict())
    cfg = cfg or await editor_config(route)
    if automatic:
        preflight_ledger = await asyncio.to_thread(db.collection('settings').document(route_key('publication_ledger',route)).get)
        if policy.automatic_reason(initial.to_dict(),cfg,now(),preflight_ledger.to_dict() or {}): return False
    if route == 'events':
        item = initial.to_dict()
        if item.get('status') != 'pending' or policy.event_reason(item,now()): return False
        try:
            fresh = await sources.refresh_event(item)
            if fresh and fresh['event'] != item.get('event'):
                await transition_item(ref,{'event':fresh['event'],'sensitive':True,'review_passed':False,
                                           'review_reason':'Данные события изменились. Проверьте обновлённые дату и место.'})
                return False
        except Exception:
            return False  # Do not publish a stale official event if the source cannot be checked.
    ledger_ref = db.collection('settings').document(route_key('publication_ledger',route))
    @firestore.transactional
    def claim(tx):
        effective_cfg = cfg
        snap = ref.get(transaction=tx)
        if not snap.exists or snap.to_dict().get('status') != 'pending': return None
        item = snap.to_dict()
        if route_of(item) != route: return None
        if route == 'events' and policy.event_reason(item,now()): return None
        ledger = ledger_ref.get(transaction=tx).to_dict() or {}
        current = now()
        if automatic:
            auto = db.collection('settings').document(route_key('autopublish',route)).get(transaction=tx)
            if not auto.exists or not auto.to_dict().get('value',False): return None
            if item.get('source_id'):
                source = db.collection('sources').document(item['source_id']).get(transaction=tx)
                if not source.exists or not source.to_dict().get('enabled',True): return None
            current_settings = db.collection('settings').document(route_key('editor',route)).get(transaction=tx)
            latest_cfg = policy.config((current_settings.to_dict() or {}).get('value',{}),route)
            if policy.automatic_reason(item,latest_cfg,current,ledger): return None
            effective_cfg = latest_cfg
        if effective_cfg['require_image'] and (effective_cfg['image_mode'] == 'off' or not (item.get('image_bytes') or item.get('image_file_id'))):
            return None
        day = current.astimezone(ZoneInfo(effective_cfg['timezone'])).date().isoformat()
        # Reserve quota before sending. Uncertain sends remain counted to prevent duplicates/limit overruns.
        tx.set(ledger_ref, {'day':day, 'count':(ledger.get('count',0) if ledger.get('day') == day else 0) + 1, 'last_at':current})
        tx.update(ref, {'status':'sending', 'claimed_at':current, 'manual':not automatic})
        return item,effective_cfg
    claimed = await asyncio.to_thread(claim, db.transaction())
    if claimed is None: return False
    item,cfg = claimed
    try:
        sent = await send_item(target_channel(route), item, cfg)
        update = {'status':'published', 'published_at':now(), 'message_id':sent['message_id'], 'channel_id':target_channel(route)}
        if sent.get('photo'):
            update.update(image_file_id=sent['photo'][-1]['file_id'], image_bytes=firestore.DELETE_FIELD)
        await asyncio.to_thread(ref.update, update)
        return True
    except Exception as error:
        await asyncio.to_thread(ref.update, {'status':'needs_reconciliation', 'error_at':now()})
        log.error('Publish uncertain, needs manual reconciliation: %s (%s)', docid, type(error).__name__)
        return False

async def reserve_image(cfg):
    ref = db.collection('settings').document(route_key('image_ledger',cfg.get('_route','news')))
    @firestore.transactional
    def reserve(tx):
        snap = ref.get(transaction=tx)
        value = snap.to_dict() or {}
        day = now().astimezone(ZoneInfo(cfg['timezone'])).date().isoformat()
        count = value.get('count', 0) if value.get('day') == day else 0
        if count >= cfg['image_daily_limit']: return False
        tx.set(ref, {'day':day, 'count':count + 1})
        return True
    return await asyncio.to_thread(reserve, db.transaction())

async def prepare_image(item, cfg):
    mode = cfg['image_mode']
    if mode == 'off': return {}
    if mode in ('source', 'source_generate') and item.get('source_images', True):
        try:
            result = await asyncio.wait_for(media.source_image(item['url'], item.get('image_candidates', [])), 25)
            if result: return result
        except Exception: pass
    errors = []
    if mode in ('generate', 'source_generate'):
        if not any(os.getenv('OPENAI_API_KEY','') if p=='openai' else GEMINI_API_KEY for p in cfg['image_order']):
            return {'image_error':'Нет подключённого ключа генератора изображений'}
        for provider in cfg['image_order']:
            key = os.getenv('OPENAI_API_KEY','') if provider == 'openai' else GEMINI_API_KEY
            if not key: continue
            if not await reserve_image(cfg):
                errors.append('Дневной лимит попыток генерации')
                break
            try:
                if provider == 'openai':
                    return await media.generate_openai(app.state.gemini_client,item,cfg['openai_image_model'],key,cfg['image_quality'])
                return await media.generate(app.state.gemini_client,item,cfg['image_model'],key)
            except Exception as error:
                log.warning('Image generation %s failed: %s',provider,type(error).__name__)
                errors.append(provider+':'+type(error).__name__)
    return {'image_error':'; '.join(errors)} if errors else {}

async def prepare_item(ref, item, cfg):
    started = time.monotonic()
    context = item.get('summary','')
    if route_of(item) == 'events': context += '\nОфіційні дані події: '+json.dumps(item.get('event',{}),default=str,ensure_ascii=False)
    ai = await asyncio.wait_for(ai_prepare(item['title'], context, item['url'], item['category'], cfg),80)
    if ai is None: raise ValueError('All configured AI writers unavailable')
    if route_of(item) == 'news' and ai['category'] == 'events':
        # An event found in a news feed belongs to the event channel and its language/settings.
        event_item = dict(item,route='events',category='events',event=item.get('event',{}),status='queued',attempts=0)
        target = db.collection('items').document(sources.identity(event_item))
        @firestore.transactional
        def reroute(tx):
            existing = target.get(transaction=tx)
            if not existing.exists: tx.set(target,event_item)
            tx.update(ref,{'status':'routed','routed_to':target.id})
        await asyncio.to_thread(reroute,db.transaction())
        return dict(item,status='routed')
    updated = dict(item, title=sanitize(ai['title'],300), text=sanitize(ai['body'],cfg['body_length']),
                   category=ai['category'], sensitive=ai['sensitive'],
                   missing_details=ai['has_missing_event_details'], review_passed=ai.get('review_passed',False),
                   ai_provider=ai.get('ai_provider',''), ai_model=ai.get('ai_model',''),
                   review_reason=ai.get('review_reason',''), review_provider=ai.get('review_provider','off'))
    updated.update(topic=sanitize(ai.get('topic',''),45),title_fi=sanitize(ai.get('title_fi',''),180),
                   text_fi=sanitize(ai.get('body_fi',''),cfg['body_length']//2))
    if route_of(item) == 'events':
        updated['category'] = 'events'
        updated['missing_details'] = bool(policy.event_reason(updated,now()))
        if ai.get('is_event') is False:
            updated.update(status='filtered',reason='Не анонс будущего события')
            await asyncio.to_thread(ref.update,updated)
            return updated
    if policy.topic_allowed(updated, cfg):
        try:
            updated.update(await asyncio.wait_for(prepare_image(updated,cfg),max(1,94-(time.monotonic()-started))))
        except asyncio.TimeoutError:
            updated['image_error'] = 'Время обработки изображения истекло; текст сохранён'
        updated['status'] = 'pending'
    else:
        updated['status'] = 'filtered'
    updated.update(prepared_at=now(), reason='')
    await asyncio.to_thread(ref.update, updated)
    return updated

async def show_failed(chat):
    lines = ['⚠️ Материалы с ошибками:']
    for status in ('awaiting_ai','processing','needs_reconciliation'):
        docs = await item_docs(status,limit=8)
        lines += [f'{d.id}: {status} — {d.to_dict().get("reason", "")}\n{d.to_dict().get("title", "")[:100]}' for d in docs]
    lines += ['/retry ID — повторить awaiting_ai после исправления ключа/модели.',
              'needs_reconciliation: сначала проверьте канал — пост мог уже уйти. Автоматического повтора нет.']
    return await say(chat,'\n'.join(lines))

async def preview(chat, docid):
    ref = db.collection('items').document(docid)
    snap = await asyncio.to_thread(ref.get)
    if not snap.exists or snap.to_dict().get('status') != 'pending': return await say(chat, 'Материал уже обработан.')
    item = snap.to_dict()
    cfg = await editor_config(route_of(item))
    if not item.get('image_bytes') and not item.get('image_file_id'):
        image = await prepare_image(item,cfg)
        if image:
            await asyncio.to_thread(ref.update,image)
            item.update(image)
    if cfg['require_image'] and not (item.get('image_bytes') or item.get('image_file_id')):
        return await say(chat,'Изображение недоступно. Проверьте ключ, модель и дневной лимит генераций.')
    sent = await send_item(chat,item,cfg)
    if sent.get('photo'):
        await asyncio.to_thread(ref.update, {'image_file_id':sent['photo'][-1]['file_id']})
    return await say(chat, 'Это превью в личном чате. В канал ничего не отправлено.')

async def collect(route=None):
    route = route or ROUTE.get()
    context = ROUTE.set(route)
    lease = db.collection('settings').document(route_key('collector_lease',route))
    token = secrets.token_hex(12)
    @firestore.transactional
    def acquire(tx):
        value = lease.get(transaction=tx).to_dict() or {}
        if value.get('until',now()-timedelta(seconds=1)) > now(): return False
        tx.set(lease,{'token':token,'until':now()+timedelta(seconds=290)})
        return True
    @firestore.transactional
    def release(tx):
        if (lease.get(transaction=tx).to_dict() or {}).get('token') == token: tx.delete(lease)
    acquired = False
    try:
        acquired = await asyncio.to_thread(acquire,db.transaction())
        if not acquired: return {'busy':True}
        result = await asyncio.wait_for(collect_batch(),timeout=240)
        await put_setting(route_key('last_collect'),dict(result,at=now().isoformat()))
        return result
    except asyncio.TimeoutError:
        return {'timeout':True,'message':'Обработка продолжится в следующем запуске.'}
    finally:
        if acquired: await asyncio.to_thread(release,db.transaction())
        ROUTE.reset(context)

async def collect_batch():
    cfg = await editor_config()
    route = ROUTE.get()
    auto = await setting(route_key('autopublish'),False)
    started = time.monotonic()
    stats = {'seen':0,'new':0,'prepared':0,'published':0,'errors':0,'filtered':0}
    if auto:
        for doc in await item_docs('pending',limit=60):
            value = doc.to_dict()
            expired = route == 'events' and value.get('event',{}).get('start') and value['event']['start'] <= now()
            if route == 'news': expired = (now()-(value.get('source_published_at') or value.get('created_at',now()))).total_seconds()>cfg['max_age_hours']*3600
            if expired:
                await transition_item(doc.reference,{'status':'expired'})
                continue
            if await publish(doc.id,automatic=True,cfg=cfg): stats['published'] += 1
            if stats['published'] >= cfg['batch_size']: break
    docs = await documents(db.collection('sources'))
    if not docs:
        await seed_catalog()
        docs = await documents(db.collection('sources'))
    active = [d for d in docs if route_of(d.to_dict()) == route and d.to_dict().get('enabled',True)
              and d.to_dict().get('category','news') in cfg['categories']]
    # Rotate source priority each run. Fetch concurrently, isolate a broken feed, keep a durable backlog.
    offset = int(await setting(route_key('source_cursor'),0) or 0) % max(1,len(active))
    active = active[offset:]+active[:offset]
    await put_setting(route_key('source_cursor'),offset+1)
    sem = asyncio.Semaphore(6)
    async def read(doc):
        async with sem:
            try:
                rows,progress = await sources.fetch(doc.to_dict(),cfg,now())
                await asyncio.to_thread(doc.reference.update,dict(last_ok=now(),last_error='',last_count=len(rows)))
                return doc,rows,progress
            except Exception as error:
                stats['errors'] += 1
                await asyncio.to_thread(doc.reference.update,{'last_error':type(error).__name__,'checked_at':now()})
                return doc,[],{}
    batches = await asyncio.gather(*(read(d) for d in active))
    # Round-robin individual items, deduplicate the same URL across thematic feeds.
    for index in range(max([len(rows) for _,rows,_ in batches]+[0])):
        for doc,rows,_ in batches:
            if index >= len(rows): continue
            raw = rows[index]; source = doc.to_dict(); stats['seen'] += 1
            if not valid_url(raw.get('url','')) or not raw.get('title'): continue
            if route == 'news' and raw.get('source_published_at') and (now()-raw['source_published_at']).total_seconds()>cfg['max_age_hours']*3600: continue
            item = dict(raw,status='queued',text='',route=route,source_id=doc.id,source_name=source.get('name',doc.id),
                        source_images=source.get('images_allowed',True),category=source.get('category','news'),created_at=now(),attempts=0)
            if route == 'events' and item.get('event',{}).get('cancelled'): continue
            ref = db.collection('items').document(sources.identity(item))
            if (await asyncio.to_thread(ref.get)).exists: continue
            await asyncio.to_thread(ref.create,item)
            stats['new'] += 1
    # Advance API pagination only after all items on this page are durably stored.
    for doc,_,progress in batches:
        if progress: await asyncio.to_thread(doc.reference.update,progress)
    retries = (await item_docs('awaiting_ai',limit=100))+(await item_docs('processing',limit=100))
    queued = await item_docs('queued',limit=400)
    # Failed drafts get a retry but cannot starve new material; cap attempts.
    work = [d for d in retries if d.to_dict().get('attempts',0)<3][:1]+queued
    attempts = 0
    for doc in work:
        if attempts >= cfg['batch_size']: break
        if time.monotonic()-started > 125: break
        item = doc.to_dict()
        if item.get('source_id'):
            source = await asyncio.to_thread(db.collection('sources').document(item['source_id']).get)
            if not source.exists or not source.to_dict().get('enabled',True):
                await asyncio.to_thread(doc.reference.update,{'status':'filtered','reason':'Источник отключён'})
                continue
        tries = item.get('attempts',0)+1
        if route == 'events' and item.get('event',{}).get('start') and item['event']['start'] <= now():
            await asyncio.to_thread(doc.reference.update,{'status':'expired'}); continue
        if route == 'news' and (now()-(item.get('source_published_at') or item.get('created_at',now()))).total_seconds()>cfg['max_age_hours']*3600:
            await asyncio.to_thread(doc.reference.update,{'status':'expired'}); continue
        attempts += 1
        await asyncio.to_thread(doc.reference.update,{'status':'processing','attempts':tries})
        item['attempts'] = tries
        try:
            updated = await asyncio.wait_for(prepare_item(doc.reference,item,cfg),timeout=100)
            stats['filtered' if updated['status']=='filtered' else 'prepared'] += 1
            if auto and updated['status']=='pending' and await publish(doc.id,automatic=True,cfg=cfg): stats['published'] += 1
        except Exception as error:
            await transition_item(doc.reference,{'status':'awaiting_ai','reason':type(error).__name__,'attempts':tries},('processing','awaiting_ai','queued'))
            stats['errors'] += 1
    return stats

async def select_context(chat, user, requested=None):
    route = requested or await selected_route(chat)
    if route not in ('news','events'): return False
    if route == 'events' and not await admin_access(user,'events'):
        await say(chat,f'Для событий нужны ваши права публикации в {EVENTS_CHANNEL_ID}.')
        return False
    ROUTE.set(route)
    if requested: await put_setting(f'route:{chat}',route)
    return True

async def authorize_item(user, docid):
    if '/' in docid or len(docid)>100: return False
    snap = await asyncio.to_thread(db.collection('items').document(docid).get)
    if not snap.exists: return False
    return route_of(snap.to_dict()) == 'news' or await admin_access(user,'events')

async def authorize_source(user, source_id):
    if '/' in source_id or len(source_id)>100: return False
    snap = await asyncio.to_thread(db.collection('sources').document(source_id).get)
    return snap.exists and (route_of(snap.to_dict()) == 'news' or await admin_access(user,'events'))

async def request_collection(chat):
    # Trigger Scheduler so Telegram's webhook finishes quickly. The work has its own HTTP request.
    import google.auth
    from google.auth.transport.requests import Request as AuthRequest
    def token():
        credentials, project = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        credentials.refresh(AuthRequest())
        return credentials.token, project
    try:
        access_token, project = await asyncio.wait_for(asyncio.to_thread(token),10)
        job = 'finland-events-collect' if ROUTE.get() == 'events' else 'finland-news-collect'
        location = os.getenv('SCHEDULER_LOCATION','europe-west1')
        response = await app.state.gemini_client.post(
            f'https://cloudscheduler.googleapis.com/v1/projects/{project}/locations/{location}/jobs/{job}:run',
            headers={'Authorization':'Bearer '+access_token},json={},timeout=10)
        response.raise_for_status()
        return await say(chat,'Сбор отправлен в Cloud Scheduler. Результат появится в /stats и /queue через несколько минут.')
    except Exception as error:
        log.warning('Manual scheduling failed: %s',type(error).__name__)
        return await say(chat,'Не удалось запустить вне расписания. Автоматический сбор продолжится по таймеру; проверьте scripts/diagnose.sh.')

async def ask_assistant(chat, question):
    cfg = await editor_config()
    await say(chat,'🤖 Готовлю ответ…')
    facts = 'Ты редакционный помощник. Отвечай кратко на языке пользователя. Не обещай выполнять действия: у тебя нет инструментов, поиска или доступа к текущим новостям. JSON: {"answer":"ответ"}. Вопрос: '+question[:3000]
    started = time.monotonic()
    for provider in cfg['ai_order']:
        if time.monotonic()-started>22: break
        if not ai_router.available().get(provider) or ai_router.cooldown(provider,cfg[provider+'_model']): continue
        try:
            response = await asyncio.wait_for(ai_router.request_json(app.state.gemini_client,provider,cfg[provider+'_model'],facts),min(cfg['ai_timeout'],8))
            if isinstance(response.get('answer'),str): return await say(chat,provider+': '+response['answer'])
        except Exception as error: ai_router.mark_failure(provider,cfg[provider+'_model'],error)
    return await say(chat,'ИИ сейчас недоступны. Проверьте ключи, модели и баланс: /settings → ИИ.')

async def handle_message(message):
    user=message.get('from',{}).get('id')
    chat_info=message.get('chat',{})
    if chat_info.get('type')!='private': return
    chat=chat_info['id']
    text=(message.get('text') or '').strip()
    if not await admin_access(user):
        if text.startswith('/start') or text=='/help':
            await say(chat, access_hint())
        return
    if text.startswith('/channel '):
        choice = text.split(maxsplit=1)[1]
        if choice not in ('news','events'): return await say(chat,'/channel news или /channel events')
        if await select_context(chat,user,choice): return await dashboard(chat)
        return
    if not await select_context(chat,user): return
    if text.startswith('/ask '): return await ask_assistant(chat,text[5:])
    if text.startswith('/event '):
        parts = [v.strip() for v in text[7:].split('|')]
        if len(parts)!=5: return await say(chat,'/event ID | 2026-11-01T18:00 | Helsinki | Место и адрес | цена')
        if not await authorize_item(user,parts[0]): return await say(chat,'Нет доступа к материалу.')
        ref = db.collection('items').document(parts[0])
        snap = await asyncio.to_thread(ref.get)
        if route_of(snap.to_dict()) != 'events': return await say(chat,'Это не событие.')
        start = sources.date(parts[1])
        if not start or start<=now(): return await say(chat,'Укажите будущую дату ISO: YYYY-MM-DDTHH:MM, время Helsinki.')
        event = dict(snap.to_dict().get('event',{}),start=start,city=parts[2][:80],venue=parts[3][:180],price=parts[4][:100],
                     is_free=parts[4].casefold() in ('0','бесплатно','безкоштовно','maksuton','free'))
        if await transition_item(ref,{'event':event,'missing_details':False,'sensitive':True,'review_passed':False}):
            return await say(chat,'Данные сохранены. Проверьте превью, затем публикуйте кнопкой.')
        return await say(chat,'Изменение разрешено только для pending.')
    edit_key=f'edit_state:{user}'
    state=await setting(edit_key) if text and not text.startswith('/') else None
    if state and await authorize_item(user,state):
        ref=db.collection('items').document(state)
        if await transition_item(ref, {'text':sanitize(text,2500),'review_passed':False,
                                       'sensitive':True,'review_reason':'Ручная правка; нужна ручная публикация'}):
            await put_setting(edit_key,None)
            return await say(chat,'✏️ Текст сохранён. Открой /queue для публикации.')
    if text == '/settings': return await show_settings(chat)
    if text == '/failed': return await show_failed(chat)
    if text.startswith('/set ') or text.startswith('/reset '):
        parts = text.split(maxsplit=2)
        try:
            if parts[0] == '/reset' and len(parts) == 2:
                value = await change_config(parts[1],policy.config(route=ROUTE.get())[parts[1]])
            elif parts[0] == '/set' and len(parts) == 3:
                value = await change_config(parts[1],parts[2])
            else: raise ValueError('Формат: /set ИМЯ значение или /reset ИМЯ')
        except (ValueError,KeyError) as error:
            return await say(chat,str(error))
        return await say(chat,f'Сохранено: {parts[1]} = {value}')
    if text.startswith('/sourceimages '):
        parts = text.split()
        if len(parts) != 3 or parts[2] not in ('on','off'): return await say(chat,'/sourceimages ID on или off')
        if not await authorize_source(user,parts[1]): return await say(chat,'Источник не найден или нет доступа.')
        ref = db.collection('sources').document(parts[1])
        if not (await asyncio.to_thread(ref.get)).exists: return await say(chat,'Источник не найден')
        await asyncio.to_thread(ref.update,{'images_allowed':parts[2]=='on'})
        return await say(chat,'Правило для будущих материалов источника обновлено. Используйте фото с разрешением правообладателя.')
    if text.startswith('/retry '):
        ref = db.collection('items').document(text.split(maxsplit=1)[1])
        if not await authorize_item(user,text.split(maxsplit=1)[1]): return await say(chat,'Нет доступа к материалу.')
        if not await transition_item(ref,{'status':'awaiting_ai','attempts':0},('awaiting_ai','filtered','pending')):
            return await say(chat,'Повтор разрешён для awaiting_ai, filtered и pending.')
        return await say(chat,'Повтор поставлен в очередь следующего сбора.')
    if text.startswith('/addsource '):
        parts=[x.strip() for x in text[len('/addsource '):].split('|')]
        if len(parts)!=3 or not valid_url(parts[1]) or parts[2] not in CATEGORIES:
            return await say(chat,'Формат: /addsource Название | https://example.com/feed.xml | news')
        source_route = 'events' if parts[2]=='events' else 'news'
        if source_route == 'events' and not await admin_access(user,'events'): return await say(chat,'Нет прав публикации в канале событий.')
        sid=hashlib.sha256((source_route+'|'+parts[1]).encode()).hexdigest()[:12]
        await asyncio.to_thread(db.collection('sources').document(sid).set, {'name':parts[0][:80],'url':parts[1],'category':parts[2],'route':source_route,'kind':'rss','enabled':False,'pending_review':True})
        return await say(chat,f'Источник добавлен как {sid}, но отключён до проверки безопасности и формата. Для активации: /enable {sid}')
    for command,enabled in (('/enable ',True),('/disable ',False)):
        if text.startswith(command):
            sid=text[len(command):].strip()
            if not await authorize_source(user,sid): return await say(chat,'Источник не найден или нет доступа.')
            ref=db.collection('sources').document(sid)
            if not (await asyncio.to_thread(ref.get)).exists: return await say(chat,'Источник не найден')
            await asyncio.to_thread(ref.update, {'enabled':enabled,'pending_review':False})
            return await say(chat,'Источник обновлён')
    if text.startswith('/deletesource '):
        sid=text[len('/deletesource '):].strip()
        if not await authorize_source(user,sid): return await say(chat,'Источник не найден или нет доступа.')
        await asyncio.to_thread(db.collection('sources').document(sid).delete)
        return await say(chat,'Источник удалён')
    if text=='/sources': return await show_sources(chat)
    if text=='/queue': return await show_queue(chat)
    if text=='/stats': return await show_stats(chat)
    if text=='/help': return await show_help(chat)
    if text=='/auto_on': await put_setting(route_key('autopublish'),True); return await dashboard(chat)
    if text=='/auto_off': await put_setting(route_key('autopublish'),False); return await dashboard(chat)
    if text=='/collect': return await request_collection(chat)
    await dashboard(chat)

async def handle_callback(callback):
    user=callback.get('from',{}).get('id')
    chat_info=callback.get('message',{}).get('chat',{})
    if chat_info.get('type')!='private': return
    chat=chat_info['id']
    try:
        await tg('answerCallbackQuery',{'callback_query_id':callback['id']})
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 400:
            raise
        try:
            description = error.response.json().get('description', '').lower()
        except ValueError:
            raise error
        if 'query is too old' in description or 'query id is invalid' in description:
            log.info('Ignoring an expired Telegram button press')
            return
        raise
    if not await admin_access(user):
        await say(chat, access_hint())
        return
    data=callback.get('data','')
    if data.startswith('route:'):
        if await select_context(chat,user,data[6:]): return await dashboard(chat)
        return
    if not await select_context(chat,user): return
    if any(data.startswith(prefix) for prefix in ('pub:','reject:','edit:','preview:')):
        if not await authorize_item(user,data.split(':',1)[1]): return await say(chat,'Нет доступа к материалу.')
    if data=='home': return await dashboard(chat)
    if data.startswith('section:'): return await show_settings(chat,data[8:])
    if data.startswith('sources:'): return await show_sources(chat,int(data[8:]))
    if data.startswith('queue:'): return await show_queue(chat,int(data[6:]))
    if data.startswith('adjust:'):
        _,key,direction = data.split(':')
        if key not in policy.RANGES or direction not in ('-1','1'): return
        cfg = await editor_config()
        step = 5 if key=='interval_minutes' else 50 if key=='body_length' else 1
        low,high = policy.RANGES[key]
        await change_config(key,max(low,min(high,cfg[key]+int(direction)*step)))
        section = next(k for k,(_,keys) in SECTIONS.items() if key in keys)
        return await show_settings(chat,section)
    if data=='settings': return await show_settings(chat)
    if data.startswith('category:'):
        key = data.split(':',1)[1]
        if key not in CATEGORIES: return
        cfg = await editor_config()
        categories = cfg['categories']
        if key in categories: categories.remove(key)
        else: categories.append(key)
        await change_config('categories',categories)
        return await show_settings(chat)
    if data.startswith('cycle:'):
        key = data.split(':',1)[1]
        if key not in policy.CHOICES and type(policy.DEFAULTS.get(key)) is not bool: return
        cfg = await editor_config()
        choices = policy.CHOICES.get(key,(False,True))
        await change_config(key,choices[(choices.index(cfg[key])+1)%len(choices)])
        return await show_settings(chat)
    if data.startswith('source:'):
        if not await authorize_source(user,data[7:]): return await say(chat,'Источник не найден или нет доступа.')
        ref = db.collection('sources').document(data[7:])
        snap = await asyncio.to_thread(ref.get)
        if snap.exists: await asyncio.to_thread(ref.update,{'enabled':not snap.to_dict().get('enabled',True),'pending_review':False})
        return await show_sources(chat)
    if data.startswith('preview:'): return await preview(chat,data[8:])
    if data=='sources': return await show_sources(chat)
    if data=='queue': return await show_queue(chat)
    if data=='help': return await show_help(chat)
    if data=='stats': return await show_stats(chat)
    if data=='toggle': await put_setting(route_key('autopublish'),not await setting(route_key('autopublish'),False)); return await show_settings(chat)
    if data=='collect': return await request_collection(chat)
    if data.startswith('pub:'): return await say(chat,'Опубликовано.' if await publish(data[4:]) else 'Не опубликовано; проверь статус.')
    if data.startswith('reject:'):
        ref=db.collection('items').document(data[7:])
        if await transition_item(ref, {'status':'rejected'}):
            return await say(chat,'Отклонено')
    if data.startswith('edit:'):
        await put_setting(f'edit_state:{user}',data[5:])
        return await say(chat,'Пришли новый текст одним сообщением. Заголовок и ссылка останутся прежними.')

@app.get('/health')
async def health(): return {'ok':True,'version':VERSION}

@app.post('/telegram')
async def telegram(request:Request):
    if not secrets.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token',''), WEBHOOK_SECRET):
        raise HTTPException(403)
    update=await request.json()
    started=time.perf_counter()
    try:
        if 'message' in update: await handle_message(update['message'])
        elif 'callback_query' in update: await handle_callback(update['callback_query'])
    except Exception as error:
        log.error('Telegram update failed: %s',type(error).__name__)
        raise HTTPException(500)
    finally:
        log.info('Telegram update %s processed in %.3fs', update.get('update_id'), time.perf_counter()-started)
    return {'ok':True}

@app.post('/collect')
async def collect_endpoint(request:Request):
    if not secrets.compare_digest(request.headers.get('X-Cron-Secret',''),CRON_SECRET): raise HTTPException(403)
    return await collect('news')

@app.post('/collect/events')
async def collect_events_endpoint(request:Request):
    if not secrets.compare_digest(request.headers.get('X-Cron-Secret',''),CRON_SECRET): raise HTTPException(403)
    return await collect('events')

@app.post('/admin/migrate')
async def migrate_endpoint(request:Request):
    if not secrets.compare_digest(request.headers.get('X-Cron-Secret',''),CRON_SECRET): raise HTTPException(403)
    added = await seed_catalog()
    # Backfill routes for legacy items; retain IDs and status to avoid reposting old material.
    updated = 0
    for doc in await documents(db.collection('items')):
        item = doc.to_dict()
        if not item.get('route'):
            item['route'] = route_of(item)
            await asyncio.to_thread(doc.reference.update,{'route':item['route']})
            # Aliases prevent a different source from reintroducing an old URL after migration.
            if item.get('url'):
                alias = db.collection('items').document(sources.identity(item))
                if alias.id != doc.id and not (await asyncio.to_thread(alias.get)).exists:
                    await asyncio.to_thread(alias.create,{'route':item['route'],'status':'duplicate','url':item['url'],'original_id':doc.id,'created_at':item.get('created_at',now())})
            updated += 1
    return {'ok':True,'sources_added':added,'legacy_items_updated':updated}
