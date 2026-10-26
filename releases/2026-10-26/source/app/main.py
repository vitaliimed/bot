"""Finland News Bot: Telegram webhook + scheduled RSS collector, Firestore storage."""
import os, re, json, html, hashlib, logging, secrets, asyncio, time, sys
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
from . import ai as ai_router, media, policy, sources, formatting, credentials, controls, publication, intake, local_editor, templates, usage, channels, workflows, jobs, observability, operations, mass, ai_diagnostics, uniqueness, dispatch

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
        credentials.store = credentials.KeyStore(db, os.getenv('GOOGLE_CLOUD_PROJECT') or db.project)
        usage.database=db
        try:
            yield
        finally:
            credentials.store = None
            usage.database=None

app = FastAPI(title='Finland News Bot', lifespan=lifespan)
BOT_TOKEN = os.environ['BOT_TOKEN']
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
ADMIN_AUTH_MODE = os.getenv('ADMIN_AUTH_MODE', 'id')
CHANNEL_ID = os.environ['CHANNEL_ID']
EVENTS_CHANNEL_ID = os.getenv('EVENTS_CHANNEL_ID', '@eventhsl')
CHANNELS={'news':{'telegram_id':CHANNEL_ID,'type':'news','name':'Новости','enabled':True},
          'events':{'telegram_id':EVENTS_CHANNEL_ID,'type':'events','name':'События','enabled':True}}
ROUTE = ContextVar('route', default='news')
UI_ACTIVE = ContextVar('ui_active', default=False)
PANEL_MESSAGE = ContextVar('panel_message', default=None)
VERSION = os.getenv('APP_VERSION', 'two-channel-editor-v2')
WEBHOOK_SECRET = os.environ['WEBHOOK_SECRET']
CRON_SECRET = os.environ['CRON_SECRET']
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite')
db = firestore.Client()
TG = f'https://api.telegram.org/bot{BOT_TOKEN}'
CATALOG = json.loads(Path(__file__).with_name('catalog.json').read_text())

def route_of(item):
    return item.get('route') or ('events' if item.get('category') == 'events' else 'news')

def route_key(key, route=None):
    chosen=route or ROUTE.get()
    return key + ('_'+chosen if chosen!='news' else '')

def target_channel(route):
    return CHANNELS.get(route,CHANNELS['news'])['telegram_id']

def channel_kind(route): return CHANNELS.get(route,{}).get('type','news')

def channel_tag(route, cfg):
    # The channel identity cannot be removed by switching off topic hashtags.
    channel = target_channel(route)
    return '#' + (channel[1:] if channel.startswith('@') else cfg.get('channel_tag','').lstrip('#') or ('eventhsl' if route == 'events' else 'suominewsua'))

async def selected_route(chat):
    value = await setting(f'route:{chat}', 'news')
    return value if value in CHANNELS and CHANNELS[value].get('enabled',True) else 'news'

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

def this(): return sys.modules[__name__]
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
    cfg = policy.config(stored,channel_kind(route))
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
    keyboard_state = None
    if parse_mode: payload.update(text=text,parse_mode=parse_mode)
    if keyboard:
        payload['reply_markup'] = {'inline_keyboard':controls.scoped(keyboard,ROUTE.get())}
    elif UI_ACTIVE.get():
        auto = await setting(route_key('autopublish'),False)
        payload['reply_markup'] = controls.keyboard(ROUTE.get(),auto)
        keyboard_state = [ROUTE.get(),bool(auto)]
    result = await tg('sendMessage', payload)
    if keyboard_state is not None: await put_setting(f'keyboard:{chat}',keyboard_state)
    return result

async def ensure_keyboard(chat):
    if not UI_ACTIVE.get(): return
    state = [ROUTE.get(),bool(await setting(route_key('autopublish'),False))]
    if await setting(f'keyboard:{chat}') != state:
        await say(chat,f'Меню: {target_channel(ROUTE.get())} • авто {"ВКЛ" if state[1] else "ВЫКЛ"}')

async def panel(chat, text, keyboard):
    """Refresh the clicked panel; old/noneditable messages get a fresh working panel."""
    message = PANEL_MESSAGE.get()
    PANEL_MESSAGE.set(None)
    if message is not None:
        try:
            return await tg('editMessageText', {'chat_id':chat,'message_id':message,'text':fit(text,4096),
                'disable_web_page_preview':True,'reply_markup':{'inline_keyboard':controls.scoped(keyboard,ROUTE.get())}})
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 400: raise
            if 'message is not modified' in error.response.text.lower(): return
    return await say(chat,text,keyboard)

async def admin_access(user_id, route=None):
    return await can_manage(user_id, mode=ADMIN_AUTH_MODE, admin_id=ADMIN_ID,
                            channel_id=target_channel(route or 'news'), telegram=tg)

def access_hint():
    if ADMIN_AUTH_MODE == 'channel_admins':
        return f'Управление доступно владельцу и администраторам канала {CHANNEL_ID} с правом публикации.'
    return 'Управление этим ботом доступно только назначенному администратору.'

async def dashboard(chat):
    auto, cfg = await asyncio.gather(setting(route_key('autopublish'),False),editor_config())
    await say(chat, f'🇫🇮 Finland Bot — {target_channel(ROUTE.get())}\n'
              f'Автопубликация: {"ВКЛ" if auto else "ВЫКЛ"}\nЯзык: {cfg["language"]}\n'
              f'Лимит: {cfg["daily_limit"]}/день • интервал {cfg["interval_minutes"]} мин.\n'
              'Кнопки управления находятся под полем ввода. /menu — восстановить меню.')
    docs=[d.to_dict() for d in await item_metadata() if route_of(d.to_dict())==ROUTE.get()]
    ready=sum(v.get('status')=='pending' for v in docs)
    failed=sum(v.get('status') in ('awaiting_ai','needs_reconciliation') for v in docs)
    return await say(chat,f'Материалов: {len(docs)} • готовых: {ready} • требуют проверки: {failed}\n'
        f'Режим расходов: {cfg["cost_mode"]} • текст: {cfg["text_mode"]}',[
        [{'text':'Все материалы / поиск','callback_data':'browse'},{'text':'Новые','callback_data':'unpublished:queued:0'}],
        [{'text':f'Готовые ({ready})','callback_data':'queue'},{'text':'Опубликованные','callback_data':'unpublished:published:0'}],
        [{'text':'Неопубликованные','callback_data':'unpublished:all:0'},{'text':'Отклонённые','callback_data':'unpublished:rejected:0'}],
        [{'text':'📢 Каналы','callback_data':'channels'},{'text':'📊 Диагностика','callback_data':'diagnose'}],
        [{'text':'Без ИИ','callback_data':'mode:off'},{'text':'Максимально бесплатно','callback_data':'mode:free'}],
        [{'text':'🛠 Обслуживание','callback_data':'operations'},{'text':'⚡ Быстрые новости','callback_data':'fastnews'}],
        [{'text':'Копия настроек','callback_data':'backup'},{'text':'Состояние Telegram','callback_data':'checktelegram'}]])

SECTIONS = {
    'schedule': ('🕒 Расписание', ['notifications','daily_limit','interval_minutes','window_start','window_end','timezone','weekdays','batch_size','processing_batch_size','dispatch_batch_size']),
    'topics': ('🎯 Темы и фильтры', ['categories','include_words','exclude_words','region_words','moderation','dedupe_hours','priority_words','max_age_hours','event_cities','event_horizon_days','free_only','event_languages']),
    'text': ('✍️ Текст', ['text_mode','language','tone','body_length','title_template','body_template','signature','hashtags','source_link','silent','channel_tag']),
    'images': ('🖼 Фото', ['image_mode','template_style','image_daily_limit','image_order','openai_image_model','image_model','cloudflare_image_model','image_quality']),
    'budget': ('💸 Режим и лимиты', ['cost_mode','ai_daily_limit','ai_monthly_limit','cloudflare_account_id','cloudflare_free_account']),
    'ai': ('🤖 ИИ и проверка', ['ai_order','translation_order','ai_timeout','review_provider']+[p+suffix for p in policy.PROVIDERS for suffix in ('_model','_backup_model')]),
}

async def show_settings(chat, section=None):
    if section == 'ai': return await show_ai(chat)
    if section == 'ai_options': section = 'ai'
    cfg, auto = await asyncio.gather(editor_config(),setting(route_key('autopublish'),False))
    title = f'⚙️ {target_channel(ROUTE.get())} • обязательная подпись {channel_tag(ROUTE.get(),cfg)}'
    keyboard = [[{'text':'Автопубликация: '+('ВКЛ → выключить' if auto else 'ВЫКЛ → включить'),'callback_data':'auto:'+str(int(not auto))}]]
    if section not in SECTIONS:
        keyboard += [[{'text':label,'callback_data':'section:'+key}] for key,(label,_) in SECTIONS.items()]
        keyboard += [[{'text':'◀️ Каналы','callback_data':'home'}]]
        return await panel(chat,title+'\nНастройки относятся только к выбранному каналу.\nРучная публикация обходит расписание и лимит постов.',keyboard)
    label, keys = SECTIONS[section]
    lines = [title,label]+[f'{k} = {",".join(map(str,cfg[k])) if isinstance(cfg[k],list) else cfg[k]}' for k in keys]
    if section == 'ai':
        lines += ['\nКлючи подключены (доступ к модели проверяется при запросе):']
        lines += [f'{p}: {"да" if configured else "нет"}; пауза {ai_router.cooldown(p,cfg[p+"_model"])} сек.' for p,configured in (await ai_router.available()).items()]
        lines += ['Ключи: /ai → провайдер → Добавить ключ. Отключение действует только в выбранном канале.']
    if section == 'budget':
        lines += ['off: ни одного запроса ИИ. free: OpenRouter :free / openrouter/free; Cloudflare только после подтверждения Workers Free. paid: платные API явно разрешены. Лимиты — число попыток, не гарантированный денежный потолок.', 'Cloudflare: укажите Account ID и подтвердите Workers Free только после проверки тарифа в кабинете.']
    if section == 'topics':
        keyboard += [[{'text':('✅ ' if key in cfg['categories'] else '⬜ ')+name,'callback_data':'category:'+key+':'+str(int(key not in cfg['categories']))}] for key,name in policy.CATEGORY_NAMES.items()]
    for key in keys:
        if key in policy.CHOICES or type(policy.DEFAULTS[key]) is bool:
            choices = policy.CHOICES.get(key,(False,True))
            value = choices[(choices.index(cfg[key])+1)%len(choices)]
            keyboard.append([{'text':controls.LABELS.get(key,key)+': '+controls.display(cfg[key])+' → '+controls.display(value),'callback_data':'value:'+key+':'+(str(int(value)) if type(value) is bool else str(choices.index(value)))}])
        elif key in policy.RANGES:
            low,high = policy.RANGES[key]
            step = 5 if key=='interval_minutes' else 50 if key=='body_length' else 1
            label = controls.LABELS.get(key,key)
            keyboard.append([{'text':'− '+label,'callback_data':'number:'+key+':'+str(max(low,cfg[key]-step))}, {'text':'+ '+label,'callback_data':'number:'+key+':'+str(min(high,cfg[key]+step))}])
        elif key!='categories':
            keyboard.append([{'text':'✏️ '+controls.LABELS.get(key,key),'callback_data':'inputsetting:'+key}])
    if section=='images': keyboard.append([{'text':'Загрузить логотип','callback_data':'dialog:logo'}])
    keyboard.append([{'text':'◀️ Разделы','callback_data':'settings'}])
    lines += ['\n/set ИМЯ значение — точное значение; /reset ИМЯ — сброс.',
              'Например: /set daily_limit 12 или /set ai_order openai,gemini,groq,deepseek,mistral,openrouter']
    return await panel(chat,'\n'.join(lines),keyboard)

async def show_sources(chat, page=0):
    docs = sorted([d for d in await documents(db.collection('sources')) if route_of(d.to_dict()) == ROUTE.get()],key=lambda d:d.id)
    page = max(0,min(page,max(0,(len(docs)-1)//8)))
    rows = docs[page*8:(page+1)*8]
    lines = [f'📡 {target_channel(ROUTE.get())}: {len(docs)} источников • страница {page+1}']
    keyboard = []
    for d in rows:
        v = d.to_dict()
        if v.get('kind') == 'manual':
            lines.append(f'🔗 {v.get("name",d.id)} — ручной импорт\n{v.get("url","")}\n{v.get("note","")}')
            keyboard.append([{'text':'Открыть '+v.get('name',d.id)[:35],'url':v['url']},
                             {'text':'➕ Добавить анонс','callback_data':'import'}])
            continue
        lines.append(f'{"🟢" if v.get("enabled",True) else "🔴"} {d.id}: {v.get("name", "")}\n{v.get("last_error") or "Последний сбор: "+str(v.get("last_ok","ещё не было"))}')
        keyboard.append([{'text':'Назначить другому каналу','callback_data':'assignsource:'+d.id}])
        keyboard.append([{'text':'Проверить '+d.id,'callback_data':'checksource:'+d.id},{'text':'Интервал','callback_data':'sourcefrequency:'+d.id}])
        keyboard.append([{'text':('Отключить ' if v.get('enabled',True) else 'Включить ')+d.id,'callback_data':'source:'+d.id+':'+str(int(not v.get('enabled',True)))}])
    keyboard.append([{'text':'←','callback_data':'sources:'+str(max(0,page-1))},{'text':'→','callback_data':'sources:'+str(page+1)}])
    keyboard.append([{'text':'➕ Добавить источник','callback_data':'dialog:source'}])
    lines += ['\n/addsource Название | https://rss-url | news (или events)', '/sourceimages ID on/off — разрешить фото источника.']
    return await panel(chat,'\n'.join(lines),keyboard)

ITEM_FIELDS = ['title','text','title_fi','text_fi','summary','url','route','status','category','created_at',
               'source_published_at','source_id','source_name','source_images','image_candidates','image_kind','image_error',
               'image_file_id','image_credit','event','sensitive','missing_details','review_passed','review_provider','review_reason',
               'ai_provider','reason','attempts','manual_review','published_to','channel_id','message_id','routed_to','original_id',
               'last_publish_reason','claimed_at','send_target','delivery_id','import_error','topic','source_page','text_method','source_language','translation_missing','image_tag','scheduled_at','content_hash','last_seen_at','published_at','unique_keys']

async def item_metadata(query=None):
    return await documents((query or db.collection('items')).select(ITEM_FIELDS))

async def item_docs(status, route=None, limit=200):
    route = route or ROUTE.get()
    docs = await item_metadata(db.collection('items').where('status','==',status))
    return sorted([d for d in docs if route_of(d.to_dict()) == route],key=lambda d:d.to_dict().get('created_at',now()))[:limit]

async def show_queue(chat, page=0):
    docs = await item_docs('pending',limit=1000)
    if not docs: return await say(chat,'Готовая очередь пуста. /unpublished — все неопубликованные; /diagnose — причины; /collect — собрать материалы.')
    page = max(0,min(page,(len(docs)-1)//4))
    for d in docs[page*4:(page+1)*4]:
        v = d.to_dict()
        keyboard = [[{'text':'✅ Опубликовать','callback_data':'pub:'+d.id},{'text':'❌ Отклонить','callback_data':'reject:'+d.id}],
                    [{'text':'✏️ Текст','callback_data':'edit:'+d.id},{'text':'🖼 Превью','callback_data':'preview:'+d.id}],
                    [{'text':'🔎 Статус / почему не опубликовано','callback_data':'status:'+d.id}]]
        event_info = policy.event_reason(v,now()) if route_of(v)=='events' else ''
        await say(chat,f'📝 {target_channel(route_of(v))} • ID {d.id}\n{v.get("title","")}\n{v.get("text","")[:650]}\n'
                  f'{v.get("title_fi","")}\n{v.get("text_fi","")[:350]}\n{v.get("url","")}\n'
                  f'Фото: {v.get("image_kind","нет")} {v.get("image_error","")}\nИИ: {v.get("ai_provider","—")}\n'
                  f'Проверка: {event_info or v.get("review_reason","")}',keyboard)
    return await say(chat,f'Страница {page+1}; всего {len(docs)}',[[{'text':'←','callback_data':'queue:'+str(max(0,page-1))},{'text':'→','callback_data':'queue:'+str(page+1)}],
        [{'text':'🗂 Все неопубликованные','callback_data':'unpublished:all:0'},{'text':'🔎 Почему не публикует','callback_data':'diagnose'}]])

async def show_stats(chat):
    docs = await item_metadata()
    counts = {}
    for d in docs:
        if route_of(d.to_dict()) != ROUTE.get(): continue
        status = d.to_dict().get('status','unknown')
        counts[status] = counts.get(status,0)+1
    run = await setting(route_key('last_collect'),{})
    return await say(chat,f'📊 {target_channel(ROUTE.get())}\n'+'\n'.join(f'{k}: {v}' for k,v in counts.items())+'\nПоследний сбор: '+str(run))

async def publication_report(route):
    from collections import Counter
    cfg = await editor_config(route)
    auto = await setting(route_key('autopublish',route),False)
    ledger = (await asyncio.to_thread(db.collection('settings').document(route_key('publication_ledger',route)).get)).to_dict() or {}
    items = [d.to_dict() for d in await item_metadata() if route_of(d.to_dict())==route]
    counts = Counter(v.get('status','unknown') for v in items)
    reasons = Counter()
    for item in items:
        if item.get('status')!='pending': continue
        probe = dict(item,_has_image=bool(item.get('image_kind') or item.get('image_file_id')))
        reasons[policy.automatic_reason(probe,cfg,now(),ledger) or 'Готово к автоматической публикации'] += 1
    source_docs = [d for d in await documents(db.collection('sources')) if route_of(d.to_dict())==route]
    connected = await ai_router.available()
    scheduled=[d.to_dict() for d in await documents(db.collection('scheduled_posts')) if d.to_dict().get('route')==route]
    return {'scheduled_jobs':dict(Counter(v.get('status','unknown') for v in scheduled)),
            'channel':target_channel(route),'auto':auto,'counts':dict(counts),'pending_reasons':dict(reasons),
            'limits':{k:cfg[k] for k in ('daily_limit','interval_minutes','window_start','window_end','max_age_hours','batch_size','processing_batch_size','require_image')},
            'writers':[p for p in cfg['ai_order'] if usage.allowed(p,cfg[p+'_model'],cfg) and connected.get(p)],
            'cost_mode':cfg['cost_mode'],'without_ai':sum(v.get('text_method')=='original' for v in items),
            'template_images':sum(v.get('image_kind')=='template' for v in items),
            'source_errors':[{'id':d.id,'error':d.to_dict()['last_error']} for d in source_docs if d.to_dict().get('last_error')],
            'active_sources':sum(d.to_dict().get('kind')!='manual' and d.to_dict().get('enabled',True) and d.to_dict().get('category','news') in cfg['categories'] for d in source_docs),
            'last_collect':await setting(route_key('last_collect',route),{})}

async def show_diagnosis(chat):
    report = await publication_report(ROUTE.get())
    lines = ['🔎 '+report['channel'],'Автопубликация: '+('ВКЛ' if report['auto'] else 'ВЫКЛ'),
             'Доступные по настройкам ИИ: '+(', '.join(report['writers']) or ('не нужны — выбран режим без ИИ' if report['cost_mode']=='off' else 'НЕТ. Бесплатный режим не разрешает подключённые платные модели; выберите «Без ИИ» или подключите бесплатного провайдера.')),
             'Задания публикации: '+str(report['scheduled_jobs']),
             'Материалы: '+', '.join(f'{k}: {v}' for k,v in report['counts'].items()),
             'Причины в готовой очереди:']
    lines += [f'• {reason}: {count}' for reason,count in report['pending_reasons'].items()]
    lines += ['Лимиты: '+str(report['limits']),f'Активных источников: {report["active_sources"]}',
              'Ошибки источников: '+str(report['source_errors'][:6]),'Последний сбор: '+str(report['last_collect']),
              'Не все найденные материалы публикуются автоматически: действуют отбор, модерация, расписание и лимиты. '
              'Любой доступный черновик можно проверить и отправить вручную из «Неопубликованные».']
    return await panel(chat,'\n'.join(lines),[[{'text':'🗂 Неопубликованные','callback_data':'unpublished:all:0'},
        {'text':'⚙️ Настройки','callback_data':'settings'}],[{'text':'🤖 ИИ','callback_data':'ai'},{'text':'🔄 Собрать','callback_data':'collect'}]])

async def show_unpublished(chat, state='all', page=0):
    filters = ('all','pending','awaiting_ai','queued','scheduled','filtered','expired','rejected','needs_reconciliation','published','duplicate')
    if state not in filters: state='all'
    docs = [d for d in await item_metadata() if route_of(d.to_dict())==ROUTE.get() and
            (d.to_dict().get('status')==state if state!='all' else d.to_dict().get('status') not in ('published','duplicate','routed'))]
    docs.sort(key=lambda d:d.to_dict().get('created_at',now()),reverse=True)
    page = max(0,min(page,max(0,(len(docs)-1)//4)))
    for doc in docs[page*4:(page+1)*4]:
        item=doc.to_dict()
        await say(chat,f'🗂 {target_channel(ROUTE.get())} • {item.get("status")}\nID: {doc.id}\n{item.get("title","")}\n'
                  f'{(item.get("text") or item.get("summary") or "")[:500]}\n{item.get("reason","")}',[
                  [{'text':'📤 Выбрать канал и опубликовать','callback_data':'pub:'+doc.id}],
                  [{'text':'🔎 Статус','callback_data':'status:'+doc.id},{'text':'✏️ Текст','callback_data':'edit:'+doc.id}]])
    keyboard = [[{'text':'←','callback_data':f'unpublished:{state}:{max(0,page-1)}'},{'text':'→','callback_data':f'unpublished:{state}:{page+1}'}]]
    labels = {'all':'Все неопубликованные','pending':'Готовые','awaiting_ai':'Ошибки ИИ','queued':'Ещё не обработаны',
              'filtered':'Отфильтрованные','expired':'Устаревшие','rejected':'Отклонённые','needs_reconciliation':'Проверить отправку','published':'История публикаций','scheduled':'Запланированные','duplicate':'Повторы'}
    keyboard += [[{'text':labels[key],'callback_data':f'unpublished:{key}:0'}] for key in filters]
    keyboard.append([{'text':'Групповые действия','callback_data':'bulkmenu'},{'text':'Поиск / все материалы','callback_data':'browse'}])
    return await say(chat,f'{target_channel(ROUTE.get())} • {labels[state]}: {len(docs)} • страница {page+1}',keyboard)

async def choose_destination(chat, user, docid):
    snap = await asyncio.to_thread(db.collection('items').document(docid).get)
    if not snap.exists: return await say(chat,'Материал не найден.')
    item = snap.to_dict()
    if item.get('status') not in publication.MANUAL_STATUSES: return await item_status(chat,docid)
    keyboard = []
    for target in CHANNELS:
        if not CHANNELS[target].get('enabled',True): continue
        if await admin_access(user,target):
            sent = target in delivery_history(item)
            keyboard.append([{'text':('✅ Уже отправлено: ' if sent else '📤 ')+target_channel(target),
                              'callback_data':'status:'+docid if sent else f'destination:{target}:{docid}'}])
    return await panel(chat,'Куда опубликовать этот материал?\n'+item.get('title','')+'\n'
                       'Перед отправкой покажу текст и предупреждения. Автопубликация для этого не нужна.',keyboard)

async def prepare_manual_send(chat, user, docid, target):
    if target not in CHANNELS or not await admin_access(user,target): return await say(chat,'Нет прав публикации в выбранном канале.')
    snap = await asyncio.to_thread(db.collection('items').document(docid).get)
    if not snap.exists or snap.to_dict().get('status') not in publication.MANUAL_STATUSES: return await item_status(chat,docid)
    item = snap.to_dict()
    if target in delivery_history(item): return await item_status(chat,docid)
    if item.get('import_error') and not item.get('text'):
        return await say(chat,'В карточке пока только ссылка. Сначала добавьте текст анонса кнопкой «Текст».')
    cfg = dict(await editor_config(target),require_image=False)
    draft = publication.draft(item,target)
    has_photo = bool(item.get('image_bytes') or item.get('image_file_id')) and cfg['image_mode']!='off'
    notes = ['Публикация в '+target_channel(target),
             'Ручное подтверждение обходит расписание, возраст, фильтры, лимиты и проверки ИИ.',
             'С изображением из карточки.' if has_photo else 'Оригинального изображения нет: будет создана бесплатная информационная карточка.']
    if not item.get('text'): notes.append('ИИ ещё не подготовил текст. Ниже исходный материал; при необходимости сначала отредактируйте его.')
    reason = policy.event_reason(item,now())
    if reason: notes.append('⚠️ '+reason+'. Подтверждайте только после проверки фактов.')
    if item.get('last_publish_reason'): notes.append('⚠️ '+item['last_publish_reason'])
    await say(chat,'\n'.join(notes))
    # The exact outgoing text/photo is shown privately before the explicit confirmation.
    await send_item(chat,draft,cfg)
    nonce = secrets.token_hex(8)
    await put_setting(f'manual_send:{user}',{'id':docid,'target':target,'nonce':nonce,'fingerprint':publication.fingerprint(item),
                                          'config':cfg,'expires':now()+timedelta(minutes=10)})
    return await say(chat,'Проверьте текст выше. Отправить в '+target_channel(target)+'?',[
        [{'text':'✅ Подтверждаю публикацию','callback_data':'sendconfirm:'+nonce}],
        [{'text':'🗓 Запланировать','callback_data':'schedule:'+nonce}],
        [{'text':'✏️ Изменить текст','callback_data':'edit:'+docid},{'text':'Отмена','callback_data':'sendcancel'}]])

async def confirm_manual_send(chat,user,nonce):
    state = await setting(f'manual_send:{user}')
    if not isinstance(state,dict) or state.get('nonce')!=nonce or state.get('expires',now())<=now():
        return await say(chat,'Подтверждение истекло или уже использовано. Снова выберите материал и канал.')
    if not await authorize_item(user,state['id']) or not await admin_access(user,state['target']):
        return await say(chat,'Нет прав для этой публикации.')
    await put_setting(f'manual_send:{user}',None)
    if dict(await editor_config(state['target']),require_image=False)!=state.get('config'):
        return await say(chat,'Настройки канала изменились после превью. Снова выберите канал, чтобы увидеть окончательный пост.')
    if state.get('scheduled_at'):
        saved=await jobs.schedule(this(),user,state)
        return await say(chat,'Публикация запланирована. Отправка — при первом запуске Scheduler после указанного времени.' if saved else 'Карточка изменилась. Повторите выбор и планирование.')
    success = await publish(state['id'],target_route=state['target'],override=True,expected_fingerprint=state['fingerprint'])
    if not success: await say(chat,'Отправка не подтверждена либо карточка изменилась. Ниже актуальный статус; при изменении текста выберите канал повторно.')
    return await item_status(chat,state['id'],'✅ ' if success else 'ℹ️ ')

async def start_import(chat,user):
    await put_setting(f'edit_state:{user}',None)
    await put_setting(f'import:{user}',{'route':ROUTE.get(),'expires':now()+timedelta(minutes=10)})
    return await say(chat,'Можно создать собственный пост с нуля, без ИИ и API-ключей. Первая строка — заголовок. Пришлите текст или фото с подписью. Если фото нет, бот создаст бесплатную карточку. Публикация — только после выбора канала и подтверждения.\nТакже можно прислать ссылку, текст анонса или перешлите пост с подписью. Фото из пересланного сообщения сохранится. '
        'Для Facebook пришлите текст и ссылку вместе: закрытые страницы бот не читает. '
        'Сначала создаётся черновик для проверки. /cancel — отмена.')

async def import_material(chat,user,message,text):
    raw_text='\n'.join(sanitize(line,5000) for line in text.splitlines())[:5000]
    links=re.findall(r'https://[^\s<>]+',text)
    for entity in message.get('entities',[])+message.get('caption_entities',[]):
        if entity.get('type')=='text_link' and valid_url(entity.get('url','')): links.append(entity['url'])
    url=next((u.rstrip('.,);') for u in links if valid_url(u.rstrip('.,);'))),'')
    origin=message.get('forward_origin',{})
    origin_chat=origin.get('chat',{})
    origin_url=''
    if origin.get('type')=='channel' and re.fullmatch(r'[A-Za-z0-9_]{5,32}',origin_chat.get('username','')) and type(origin.get('message_id')) is int:
        origin_url=f'https://t.me/{origin_chat["username"]}/{origin["message_id"]}'
    supplied=raw_text
    for link in links: supplied=supplied.replace(link,'').strip()
    if not supplied and not url and not origin_url:
        return await say(chat,'Добавьте текст или публичную HTTPS-ссылку к изображению.')
    item={'title':supplied.split('\n')[0][:180] or 'Анонс для проверки','summary':supplied,'event':{},'image_candidates':[],'url':url or origin_url}
    if url and not supplied:
        try: item=await intake.import_url(url)
        except Exception:
            item.update(import_error='Страница недоступна. Пришлите текст анонса кнопкой «Текст».')
    if supplied: item.update(summary=supplied)
    item.update(route=ROUTE.get(),category='events' if ROUTE.get()=='events' else 'news',status='pending',
                text='\n'.join(sanitize(line,2500) for line in item.get('summary','').splitlines())[:2500],text_method='original',ai_provider='',manual_review=True,sensitive=True,review_passed=False,
                source_name=origin_chat.get('title') or (urlparse(url).hostname if url else 'Добавлено редактором'),
                source_page=origin_url,created_at=now(),attempts=0,source_images=True)
    if message.get('photo'):
        item.update(image_file_id=message['photo'][-1]['file_id'],image_kind='source')
    identity=sources.identity(item) if item.get('url') else hashlib.sha256((ROUTE.get()+'|manual|'+str(user)+'|'+text).encode()).hexdigest()[:28]
    ref=db.collection('items').document(identity)
    if not (await asyncio.to_thread(ref.get)).exists:
        try: await asyncio.to_thread(ref.create,item)
        except Exception as error:
            if type(error).__name__!='AlreadyExists': raise
    await put_setting(f'import:{user}',None)
    await say(chat,'Черновик сохранён. Проверьте факты и язык; «Превью / фото» подберёт изображение, «Выбрать канал» покажет окончательный пост.')
    return await item_status(chat,identity)

async def reconcile_send(chat,user,docid,confirmed=False):
    if not await authorize_item(user,docid): return await say(chat,'Нет доступа к материалу.')
    ref=db.collection('items').document(docid)
    item=(await asyncio.to_thread(ref.get)).to_dict() or {}
    target=item.get('send_target') or route_of(item)
    if not await admin_access(user,target): return await say(chat,'Нет прав публикации в канале отправки.')
    if item.get('status') not in ('sending','needs_reconciliation') or target in delivery_history(item):
        return await item_status(chat,docid)
    if item.get('status')=='sending' and (now()-item.get('claimed_at',now())).total_seconds()<600:
        return await say(chat,'Отправка ещё может выполняться. Подождите 10 минут, затем проверьте канал и статус.')
    state_key=f'reconcile:{user}'
    if not confirmed:
        await put_setting(state_key,{'id':docid,'fingerprint':publication.fingerprint(item),'expires':now()+timedelta(minutes=5)})
        return await say(chat,'Откройте '+target_channel(target)+' и убедитесь, что этого поста там НЕТ. '
            'Если он есть, повторять отправку нельзя. Подтверждение ниже только разблокирует карточку, не публикует её.',[
            [{'text':'Проверил канал: поста нет','callback_data':'reconcileok:'+docid}],
            [{'text':'Назад к статусу','callback_data':'status:'+docid}]])
    state=await setting(state_key)
    if not isinstance(state,dict) or state.get('id')!=docid or state.get('expires',now())<=now():
        return await say(chat,'Сначала откройте статус и подтвердите проверку канала.')
    @firestore.transactional
    def reset(tx):
        current=ref.get(transaction=tx).to_dict() or {}
        if publication.fingerprint(current)!=state['fingerprint'] or target in delivery_history(current): return False
        delivery=db.collection('deliveries').document(current.get('delivery_id') or publication.delivery_id(current,docid,target))
        unique_refs=[db.collection('story_locks').document(k) for k in current.get('unique_keys',[])]
        unique_records=[r.get(transaction=tx).to_dict() or {} for r in unique_refs]
        for unique_ref,record in zip(unique_refs,unique_records):
            if record.get('item_id')==docid:tx.set(unique_ref,dict(record,status='retry_allowed'))
        tx.set(delivery,{'status':'retry_allowed','item_id':docid,'route':target,'checked_by':user,'checked_at':now()})
        tx.update(ref,{'status':'published' if delivery_history(current) else 'pending','last_publish_reason':'Редактор проверил канал: отправка не найдена'})
        return True
    changed=await asyncio.to_thread(reset,db.transaction())
    await put_setting(state_key,None)
    await say(chat,'Карточка разблокирована. Снова выберите канал и подтвердите публикацию.' if changed else 'Статус уже изменился; обновите карточку.')
    return await item_status(chat,docid)

async def show_help(chat):
    return await say(chat,'/start или /menu — постоянное меню\n/channel news или /channel events — выбрать канал\n/settings — разделы настроек; /set ИМЯ значение\n'
        '/ai — провайдеры, ключи, порядок и модели\n/status ID — статус конкретного материала\n/cancel — отменить ввод ключа, модели или текста\n'
        '/sources /queue /unpublished /archive /diagnose /stats /failed /collect\n'
        '/import — добавить ссылку, текст или пересланный пост\n/auto_on /auto_off — выбранный канал\n/retry ID — повторить обработку\n'
        '/event ID | 2026-11-01T18:00 | Helsinki | Место и адрес | бесплатно или цена — проверенные данные события\n'
        '/ask вопрос — редакционный помощник ИИ (не меняет настройки и не публикует)\n'
        '/addsource Название | https://rss-url | events\n/enable ID /disable ID /deletesource ID\n'
        'Фото: источник → генерация. Если ИИ недоступен, нет даты или превышен лимит, материал остаётся для проверки.\n'
        'Для управления событиями нужны права публикации в обоих каналах.')

AI_NAMES = {'openai':'OpenAI / ChatGPT','gemini':'Google Gemini','groq':'Groq',
            'deepseek':'DeepSeek','mistral':'Mistral','openrouter':'OpenRouter','cloudflare':'Cloudflare Workers AI','huggingface':'Hugging Face'}
AI_KEY_URLS = {'openai':'https://platform.openai.com/api-keys','gemini':'https://aistudio.google.com/api-keys',
               'groq':'https://console.groq.com/keys','deepseek':'https://platform.deepseek.com/',
               'mistral':'https://console.mistral.ai/','openrouter':'https://openrouter.ai/keys','cloudflare':'https://dash.cloudflare.com/profile/api-tokens','huggingface':'https://huggingface.co/settings/tokens'}

async def key_access(user):
    if ADMIN_AUTH_MODE == 'id': return type(user) is int and ADMIN_ID > 0 and user == ADMIN_ID
    if ADMIN_AUTH_MODE != 'channel_admins': return False
    try:
        member = await tg('getChatMember',{'chat_id':CHANNEL_ID,'user_id':user})
        return (member.get('status') == 'creator' and member.get('user',{}).get('id') == user
                and not member.get('user',{}).get('is_bot'))
    except Exception:
        return False

async def show_ai(chat, provider=None):
    cfg = await editor_config()
    connected = await ai_router.available()
    title = f'🤖 ИИ • {target_channel(ROUTE.get())}'
    if provider not in policy.PROVIDERS:
        lines = [title,'Порядок текста: '+' → '.join(cfg['ai_order']),
                 'Ключи общие для каналов. Режим: '+cfg['cost_mode']+'. Без ИИ: настройки → Текст → original; расходы → off.']
        keyboard = []
        for p in cfg['ai_order'] + [p for p in policy.PROVIDERS if p not in cfg['ai_order']]:
            label = '⏸ отключён' if p in cfg['disabled_ai'] else '🟢 ключ есть' if connected.get(p) else '🔑 нет ключа'
            keyboard.append([{'text':AI_NAMES[p]+' • '+label,'callback_data':'ai:'+p}])
        keyboard += [[{'text':'💸 Режим расходов / без ИИ','callback_data':'section:budget'}],
                     [{'text':'📊 Расходы и экономия','callback_data':'usage'}],
                     [{'text':'⚙️ Порядок, проверяющий, таймаут','callback_data':'section:ai_options'}],
                     [{'text':'◀️ Настройки','callback_data':'settings'}]]
        return await panel(chat,'\n'.join(lines),keyboard)
    enabled = provider not in cfg['disabled_ai']
    info = await credentials.describe(provider)
    origin = {'secret':'подключён через управление ключами','environment':'из Cloud Run',
              'disabled':'подключение удалено','missing':'не подключён'}.get(info['mode'],'не подключён')
    lines = [title,AI_NAMES[provider],f'Использование в этом канале: {"ВКЛ" if enabled else "ВЫКЛ"}',
             'Ключ: '+origin,f'Модель текста: {cfg[provider+"_model"]}',
             'Место в очереди: '+(str(cfg['ai_order'].index(provider)+1) if provider in cfg['ai_order'] else 'не используется для текста'),
             f'Пауза после ошибки: {ai_router.cooldown(provider,cfg[provider+"_model"])} сек.',
             'Наличие ключа не подтверждает баланс и доступ к модели. «Проверить» делает небольшой запрос к API.',
             'Создать ключ: '+AI_KEY_URLS[provider]]
    last_test=await setting(route_key('ai_test:'+provider))
    if isinstance(last_test,dict):lines.append('Последняя проверка: '+str(last_test.get('at'))+' — '+last_test.get('reason',''))
    issue=ai_diagnostics.preflight(provider,cfg)
    if issue:lines.append('ℹ️ '+issue)
    if cfg['review_provider'] == provider and not enabled:
        lines.append('Этот ИИ назначен проверяющим, но отключён. Автопубликация ждёт проверки; выберите другого проверяющего в настройках.')
    keyboard = [
        [{'text':'⏸ Отключить в этом канале' if enabled else '▶️ Включить в этом канале','callback_data':f'aienable:{provider}:{int(not enabled)}'}],
        [{'text':'⬆️ Сделать первым','callback_data':'aifirst:'+provider},{'text':'✏️ Модель текста','callback_data':'aimodel:'+provider}],
        [{'text':'🧪 Проверить текстовый ИИ','callback_data':'aitest:'+provider}],
        [{'text':'🔑 Добавить / заменить ключ','callback_data':'keyadd:'+provider}],
        [{'text':'🗑 Удалить подключение в обоих каналах','callback_data':'keyremove:'+provider}],
        [{'text':'◀️ Все ИИ','callback_data':'ai'}],
    ]
    return await panel(chat,'\n'.join(lines),keyboard)

async def start_input(chat, user, kind, provider):
    credentials.validate_provider(provider)
    if kind == 'key':
        if not await key_access(user):
            return await say(chat,f'Ключами управляет владелец {CHANNEL_ID}. Настройки ИИ канала доступны его администраторам.')
        if os.getenv('AI_KEY_MANAGEMENT') != '1':
            return await say(chat,'Сначала обновите сервер: bash scripts/upgrade-all.sh. Это настроит хранение ключей.')
    await put_setting(f'edit_state:{user}',None)
    await put_setting(f'input:{user}',{'kind':kind,'provider':provider,'route':ROUTE.get(),
                                    'expires':now()+timedelta(minutes=5),'nonce':secrets.token_hex(8)})
    if kind == 'key':
        return await say(chat,f'🔑 {AI_NAMES[provider]}: пришлите следующим сообщением только API-ключ.\n'
            'Ключ используется обоими каналами. Бот сохранит его в Secret Manager и попытается удалить ваше сообщение. '
            'Telegram — облачный чат; если не хотите передавать ключ здесь, используйте Cloud Shell: bash scripts/connect-ai.sh '+provider+'\n'
            'Ввод действует 5 минут. /cancel — отменить.\nСоздать ключ: '+AI_KEY_URLS[provider])
    return await say(chat,f'Пришлите точный ID текстовой модели {AI_NAMES[provider]} для {target_channel(ROUTE.get())}. '
                          'Например, ID из кабинета провайдера. /cancel — отменить.')

async def take_input(user, expected):
    ref = db.collection('settings').document(f'input:{user}')
    @firestore.transactional
    def claim(tx):
        snap = ref.get(transaction=tx)
        value = (snap.to_dict() or {}).get('value') if snap.exists else None
        if not isinstance(value,dict) or value.get('nonce') != expected.get('nonce'): return False
        tx.set(ref,{'value':None})
        return True
    return await asyncio.to_thread(claim,db.transaction())

async def erase_key_message(message):
    try:
        if not message.get('message_id'): return False
        await tg('deleteMessage',{'chat_id':message['chat']['id'],'message_id':message['message_id']})
        return True
    except Exception:
        return False

async def consume_input(message, state):
    user,chat = message['from']['id'],message['chat']['id']
    kind = state.get('kind')
    if kind not in ('key','model'): return False
    text = (message.get('text') or '').strip()
    erased = await erase_key_message(message) if kind == 'key' else True
    if not await take_input(user,state):
        await say(chat,'Этот ввод уже обработан. Откройте /ai для следующего действия.')
        return True
    expires = state.get('expires')
    if not isinstance(expires,datetime) or expires < now():
        await say(chat,'Время ввода истекло. Откройте /ai и снова выберите действие.' +
                  (' Сообщение с ключом не удалось удалить; удалите его вручную.' if not erased else ''))
        return True
    if kind == 'key':
        if not await key_access(user):
            await say(chat,'Нет прав на изменение ключа. Обратитесь к владельцу основного канала.')
            return True
        try:
            if credentials.store is None or os.getenv('AI_KEY_MANAGEMENT') != '1': raise RuntimeError('Key management unavailable')
            await credentials.store.save(state['provider'],text,user)
        except ValueError:
            await say(chat,'Ключ не сохранён: нужен только API-ключ, без команд, кавычек и пробелов. Откройте /ai и повторите ввод.' +
                      (' Удалите сообщение с ключом вручную.' if not erased else ''))
            return True
        except Exception as error:
            log.warning('Credential save failed: %s',type(error).__name__)
            await say(chat,'Не удалось сохранить подключение. Старое подключение сохранено, если обновление не завершилось. '
                      'Проверьте /ai; при ошибке прав запустите scripts/upgrade-all.sh.' +
                      (' Удалите сообщение с ключом вручную.' if not erased else ''))
            return True
        await say(chat,'Ключ сохранён для обоих каналов. Перезапуск сервера не нужен. '
                  'Нажмите «Проверить текстовый ИИ», чтобы проверить модель и доступ.' +
                  (' Telegram не удалил сообщение с ключом — удалите его вручную.' if not erased else ''))
    else:
        if credentials.looks_like_key(text):
            erased = await erase_key_message(message)
            await say(chat,'Это похоже на ключ, а ожидался ID модели. Ничего не сохранено. '
                      'Для ключа: /ai → Добавить ключ.'+(' Удалите сообщение вручную.' if not erased else ''))
            return True
        if not await select_context(chat,user,state.get('route')): return True
        try:
            await change_config(state['provider']+'_model',text)
        except ValueError as error:
            await say(chat,str(error)+' Откройте /ai и повторите ввод.')
            return True
        await say(chat,'Модель сохранена для '+target_channel(ROUTE.get())+'.')
    if kind == 'key' and not await select_context(chat,user,state.get('route')): return True
    await show_ai(chat,state['provider'])
    return True

async def ai_action(chat, user, data):
    action, _, value = data.partition(':')
    provider, _, flag = value.partition(':')
    if action == 'ai': return await show_ai(chat,provider)
    credentials.validate_provider(provider)
    if action == 'aienable':
        if flag not in ('0','1'): return
        cfg = await editor_config()
        disabled = [p for p in cfg['disabled_ai'] if p != provider]
        if flag == '0': disabled.append(provider)
        await change_config('disabled_ai',disabled)
    elif action == 'aifirst':
        cfg = await editor_config()
        await change_config('ai_order',[provider]+[p for p in cfg['ai_order'] if p!=provider])
        await change_config('disabled_ai',[p for p in cfg['disabled_ai'] if p!=provider])
    elif action == 'aimodel': return await start_input(chat,user,'model',provider)
    elif action == 'keyadd': return await start_input(chat,user,'key',provider)
    elif action in ('keyremove','keyconfirm'):
        if not await key_access(user): return await say(chat,f'Ключами управляет владелец {CHANNEL_ID}.')
        if action == 'keyremove':
            nonce = secrets.token_hex(8)
            await put_setting(f'input:{user}',{'kind':'remove','provider':provider,'nonce':nonce,'expires':now()+timedelta(minutes=5)})
            return await panel(chat,f'Удалить подключение {AI_NAMES[provider]} для ОБОИХ каналов?\n'
                'Новые запросы бота перестанут использовать ключ. Уже начатые запросы могут завершиться. '
                'Ключ в кабинете провайдера и история версий в Secret Manager останутся. '
                'Для отключения только в этом канале используйте «Отключить в этом канале».',[
                    [{'text':'Да, удалить подключение','callback_data':f'keyconfirm:{provider}:{nonce}'}],
                    [{'text':'Отмена','callback_data':'inputcancel'}]])
        state = await setting(f'input:{user}')
        if (not isinstance(state,dict) or state.get('kind')!='remove' or state.get('provider')!=provider
                or state.get('nonce')!=flag or not isinstance(state.get('expires'),datetime) or state['expires']<now()
                or not await take_input(user,state)):
            return await say(chat,'Подтверждение истекло или уже использовано. Откройте /ai.')
        if credentials.store is None: return await say(chat,'Хранение ключей недоступно; обновите сервер.')
        await credentials.store.remove(provider,user)
        await say(chat,'Подключение удалено для обоих каналов. Старый ключ из Cloud Run автоматически не включится.')
    elif action == 'aitest':
        cfg = await editor_config()
        blocked=ai_diagnostics.preflight(provider,cfg)
        if blocked:return await say(chat,'ℹ️ '+AI_NAMES[provider]+': '+blocked)
        try:
            if not (await credentials.describe(provider)).get('configured'):return await say(chat,'Сначала подключите API-ключ.')
            result = await asyncio.wait_for(ai_router.execute(app.state.gemini_client,provider,cfg[provider+'_model'],
                'Reply with JSON only: {"ok": true}',cfg,'test'),min(cfg['ai_timeout'],15))
            if result.get('ok') is not True: raise ValueError('Unexpected response')
            await put_setting(route_key('ai_test:'+provider),{'at':now(),'ok':True,'reason':'Модель отвечает'})
            ai_router.COOLDOWNS.pop((provider,cfg[provider+'_model']),None)
            await say(chat,'✅ '+AI_NAMES[provider]+': текстовая модель отвечает. Генерация картинок этой проверкой не проверяется.')
        except Exception as error:
            if not isinstance(error,usage.BudgetBlocked):ai_router.mark_failure(provider,cfg[provider+'_model'],error)
            explanation=ai_diagnostics.explain(error)
            try:await put_setting(route_key('ai_test:'+provider),{'at':now(),'ok':False,'reason':explanation})
            except Exception:pass
            return await say(chat,'❌ '+AI_NAMES[provider]+': '+explanation)

    await show_ai(chat,provider)

async def item_status(chat, docid, prefix=''):
    snap = await asyncio.to_thread(db.collection('items').document(docid).get)
    if not snap.exists: return await say(chat,'Материал не найден. Откройте /queue.')
    item = snap.to_dict()
    route = route_of(item)
    cfg = await editor_config(route)
    status = item.get('status','unknown')
    labels = {'pending':'Ожидает публикации','published':'Опубликовано','sending':'Отправляется',
              'needs_reconciliation':'Результат отправки не подтверждён','awaiting_ai':'Ожидает обработки ИИ',
              'queued':'Ожидает подготовки','scheduled':'Запланировано','processing':'Обрабатывается','filtered':'Отфильтровано',
              'rejected':'Отклонено','expired':'Устарело','duplicate':'Дубликат','routed':'Перенесено в события'}
    lines = [prefix+labels.get(status,status),target_channel(route),f'ID: {docid}',item.get('title','')[:180]]
    keyboard = []
    if status == 'pending':
        reason = policy.event_reason(item,now())
        if not reason and cfg['require_image'] and (cfg['image_mode']=='off' or not(item.get('image_bytes') or item.get('image_file_id'))):
            lines.append('Изображение: при отправке будет создана бесплатная карточка. Ключ ИИ не требуется.')
        if reason: lines.append('Проверка: '+reason)
        if item.get('last_publish_reason'): lines.append('Последняя попытка: '+item['last_publish_reason'])
        ledger = await asyncio.to_thread(db.collection('settings').document(route_key('publication_ledger',route)).get)
        auto = await setting(route_key('autopublish',route),False)
        blocked = policy.automatic_reason(item,cfg,now(),ledger.to_dict() or {}) if auto else 'Автопубликация выключена'
        lines.append('Авто: '+(blocked or 'ожидает следующего сбора'))
    elif status == 'needs_reconciliation':
        lines.append('Сначала проверьте канал: Telegram мог уже принять пост. Повтор заблокирован, чтобы не создать дубликат.')
    for delivered in delivery_history(item).values():
        if str(delivered.get('channel_id','')).startswith('@') and delivered.get('message_id'):
            lines.append('https://t.me/'+delivered['channel_id'][1:]+'/'+str(delivered['message_id']))
    if status in publication.MANUAL_STATUSES:
        lines.append('Ручная публикация: выберите канал, проверьте превью и подтвердите отправку.')
        keyboard.append([{'text':'☑️ В группу / убрать','callback_data':'select:'+docid}])
        keyboard.append([{'text':'📤 Выбрать канал','callback_data':'pub:'+docid}])
        if status!='published':
            keyboard.append([{'text':'🖼 Превью / фото','callback_data':'preview:'+docid},{'text':'✏️ Текст','callback_data':'edit:'+docid}])
            keyboard.append([{'text':'✏️ Заголовок','callback_data':'title:'+docid}])
            keyboard.append([{'text':'🔄 Подготовить заново','callback_data':'retry:'+docid},{'text':'🖼 Заменить фото','callback_data':'photo:'+docid}])
            keyboard.append([{'text':'❌ Отклонить','callback_data':'reject:'+docid}])
    elif status in ('needs_reconciliation','sending'):
        keyboard.append([{'text':'Проверить отправку в канале','callback_data':'reconcile:'+docid}])
    if item.get('scheduled_at'): lines.append('Запланировано: '+str(item['scheduled_at']))
    if item.get('reason'): lines.append('Причина: '+str(item['reason'])[:300])
    if status!='pending' and item.get('last_publish_reason'): lines.append('Последняя попытка: '+item['last_publish_reason'])
    if item.get('image_error'): lines.append('Фото: '+item['image_error'])
    if item.get('import_error'): lines.append(item['import_error'])
    keyboard.append([{'text':'🔄 Обновить статус','callback_data':'status:'+docid}])
    return await panel(chat,'\n'.join(lines),keyboard)

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
    item=dict(item)
    if (not item.get('image_file_id') and not item.get('image_bytes')) or cfg['image_mode'] in ('off','template') or (item.get('image_kind')=='template' and item.get('image_tag')!=channel_tag(route_of(item),cfg)):
        item.pop('image_file_id',None)
        item.update(await local_image(item,cfg))
    if item.get('image_file_id') or item.get('image_bytes'):
        payload = {'chat_id':chat,'caption':format_post(item,cfg,photo=True),'parse_mode':'HTML','disable_notification':cfg['silent']}
        if item.get('image_file_id'):
            payload['photo'] = item['image_file_id']
            sent=await tg('sendPhoto',payload)
        else: sent=await tg('sendPhoto',payload,files={'photo':('news.jpg',item['image_bytes'],'image/jpeg')})
        return dict(sent,_image_kind=item.get('image_kind',''),_image_tag=item.get('image_tag',''))
    raise ValueError('Cannot create required image')

async def publication_block(ref, reason):
    await transition_item(ref,{'last_publish_reason':reason,'last_publish_attempt':now()})
    return False

def delivery_history(item):
    history = dict(item.get('published_to') or {})
    if item.get('status') == 'published' and item.get('message_id'):
        channel = item.get('channel_id') or target_channel(route_of(item))
        for route in CHANNELS:
            if channel == target_channel(route): history.setdefault(route,{'message_id':item['message_id'],'channel_id':channel})
    return history

async def publish(docid, automatic=False, cfg=None, target_route=None, override=False, expected_fingerprint=None):
    if (target_route is not None and target_route not in CHANNELS) or (automatic and (override or target_route is not None)): return False
    if os.getenv('DRY_RUN','0')=='1': return False
    ref = db.collection('items').document(docid)
    initial = await asyncio.to_thread(ref.get)
    if not initial.exists: return False
    original = initial.to_dict()
    route = target_route or route_of(original)
    if not original.get('title'): return False
    if original.get('original_id'):
        canonical=(await asyncio.to_thread(db.collection('items').document(original['original_id']).get)).to_dict() or {}
        if route in delivery_history(canonical):
            await asyncio.to_thread(ref.update,{'last_publish_reason':'Оригинал уже опубликован в этом канале: '+original['original_id']})
            return False
    if expected_fingerprint and publication.fingerprint(original) != expected_fingerprint: return False
    if not CHANNELS.get(route,{}).get('enabled',True): return False
    if (channel_kind(route)=='events' or original.get('category')=='events' or original.get('event')) and (policy.event_expired(original,now()) or original.get('event',{}).get('cancelled')):
        await asyncio.to_thread(ref.update,{'last_publish_reason':'Нельзя отправить истёкшее или отменённое событие как предстоящее'})
        return False
    cfg = cfg or await editor_config(route)
    if override:
        cfg = dict(cfg,require_image=False)
        if not original.get('image_bytes') and not original.get('image_file_id'): cfg['image_mode'] = 'off'
    allowed = publication.MANUAL_STATUSES if override else ('pending',)
    if automatic:
        preflight_ledger = await asyncio.to_thread(db.collection('settings').document(route_key('publication_ledger',route)).get)
        if policy.automatic_reason(initial.to_dict(),cfg,now(),preflight_ledger.to_dict() or {}): return False
    if channel_kind(route) == 'events' and not override:
        item = initial.to_dict()
        if item.get('status') != 'pending': return False
        reason = policy.event_reason(item,now())
        if reason: return await publication_block(ref,reason)
        try:
            fresh = await sources.refresh_event(item)
            if fresh and fresh['event'] != item.get('event'):
                await transition_item(ref,{'event':fresh['event'],'sensitive':True,'review_passed':False,
                                           'review_reason':'Данные события изменились. Проверьте обновлённые дату и место.',
                                           'last_publish_reason':'Источник изменил данные события. Откройте превью, проверьте дату и место, затем публикуйте.'})
                return False
        except Exception:
            return await publication_block(ref,'Источник события не ответил при проверке. Повторите публикацию позже.')
    ledger_ref = db.collection('settings').document(route_key('publication_ledger',route))
    delivery_key = publication.delivery_id(original,docid,route)
    delivery_ref = db.collection('deliveries').document(delivery_key)
    unique_refs=[db.collection('story_locks').document(k) for k in uniqueness.keys(original,route)]
    @firestore.transactional
    def claim(tx):
        effective_cfg = cfg
        snap = ref.get(transaction=tx)
        if not snap.exists or snap.to_dict().get('status') not in allowed: return None
        item = snap.to_dict()
        if expected_fingerprint and publication.fingerprint(item) != expected_fingerprint: return None
        if not override and route_of(item) != route: return None
        if not override and channel_kind(route) == 'events' and policy.event_reason(item,now()): return None
        history = delivery_history(item)
        if route in history: return None
        previous_delivery = delivery_ref.get(transaction=tx)
        if previous_delivery.exists and previous_delivery.to_dict().get('status') in ('sending','published','needs_reconciliation'):
            previous=previous_delivery.to_dict()
            tx.update(ref,{'last_publish_reason':'Этот материал уже отправлен или проверяется в выбранном канале. ID карточки: '+previous.get('item_id',docid)})
            return None
        if publication.fingerprint(item)!=publication.fingerprint(original):return None
        unique_records=[r.get(transaction=tx).to_dict() or {} for r in unique_refs]
        conflict=next((v for v in unique_records if uniqueness.blocked(v,now()) and v.get('item_id')!=docid),None)
        if conflict:
            tx.update(ref,{'last_publish_reason':'Совпадение заголовка или исходного текста с отправленным материалом: '+conflict.get('item_id','')})
            return None
        ledger = ledger_ref.get(transaction=tx).to_dict() or {}
        current = now()
        if automatic:
            auto = db.collection('settings').document(route_key('autopublish',route)).get(transaction=tx)
            if not auto.exists or not auto.to_dict().get('value',False): return None
            if item.get('source_id'):
                source = db.collection('sources').document(item['source_id']).get(transaction=tx)
                if not source.exists or not source.to_dict().get('enabled',True): return None
            current_settings = db.collection('settings').document(route_key('editor',route)).get(transaction=tx)
            latest_cfg = policy.config((current_settings.to_dict() or {}).get('value',{}),channel_kind(route))
            if policy.automatic_reason(item,latest_cfg,current,ledger): return None
            effective_cfg = latest_cfg
        day = current.astimezone(ZoneInfo(effective_cfg['timezone'])).date().isoformat()
        # Reserve quota before sending. Uncertain sends remain counted to prevent duplicates/limit overruns.
        tx.set(ledger_ref, {'day':day, 'count':(ledger.get('count',0) if ledger.get('day') == day else 0) + 1, 'last_at':current})
        for unique_ref in unique_refs:tx.set(unique_ref,uniqueness.reservation(docid,route,current,effective_cfg['dedupe_hours']))
        tx.set(delivery_ref,{'status':'sending','item_id':docid,'route':route,'claimed_at':current})
        tx.update(ref, {'status':'sending', 'claimed_at':current, 'manual':not automatic, 'last_publish_reason':'',
                        'delivery_id':delivery_key,'send_target':route,'published_to':history,'unique_keys':[r.id for r in unique_refs]})
        item = dict(item,published_to=history)
        return publication.draft(item,route) if override else item,effective_cfg
    claimed = await asyncio.to_thread(claim, db.transaction())
    if claimed is None: return False
    item,cfg = claimed
    try:
        sent = await send_item(target_channel(route), item, cfg)
        history = dict(item.get('published_to') or {})
        history[route] = {'message_id':sent['message_id'],'channel_id':target_channel(route),'published_at':now()}
        update = {'status':'published', 'published_at':now(), 'message_id':sent['message_id'], 'channel_id':target_channel(route),'published_to':history}
        if sent.get('_image_kind'):update.update(image_kind=sent['_image_kind'],image_tag=sent.get('_image_tag',''))
        if sent.get('photo'):
            update.update(image_file_id=sent['photo'][-1]['file_id'], image_bytes=firestore.DELETE_FIELD)
        await asyncio.to_thread(ref.update, update)
        await asyncio.to_thread(delivery_ref.set,{'status':'published','item_id':docid,'route':route,**history[route]})
        for unique_ref in unique_refs:await asyncio.to_thread(unique_ref.set,uniqueness.reservation(docid,route,now(),cfg['dedupe_hours'],'published'))
        return True
    except Exception as error:
        await asyncio.to_thread(ref.update, {'status':'needs_reconciliation', 'error_at':now()})
        await asyncio.to_thread(delivery_ref.set,{'status':'needs_reconciliation','item_id':docid,'route':route,'error_at':now()})
        await observability.error(this(),'Публикация',route,getattr(getattr(error,'response',None),'status_code',type(error).__name__),item_id=docid,recommendation='Проверьте канал: Telegram мог принять пост. Затем откройте статус материала.',notify=True)
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
    if mode in ('off','template'): return await local_image(item,cfg)
    if mode in ('source', 'source_generate','source_template') and item.get('source_images', True):
        try:
            result = await asyncio.wait_for(media.source_image(item['url'], item.get('image_candidates', [])), 25)
            if result: return result
        except Exception: pass
    errors = []
    if mode in ('generate', 'source_generate'):
        connected = await ai_router.available()
        for provider in cfg['image_order']:
            model=cfg['openai_image_model'] if provider=='openai' else cfg['cloudflare_image_model'] if provider=='cloudflare' else cfg['image_model']
            if not usage.allowed(provider,model,cfg) or not connected.get(provider): continue
            try:
                key = await credentials.get_key(provider)
            except Exception as error:
                errors.append(provider+': не удалось прочитать ключ')
                continue
            if not key: continue
            if not await reserve_image(cfg):
                errors.append('Дневной лимит попыток генерации')
                break
            request_id=None
            try:
                request_id=await usage.reserve(provider,model,cfg,'image')
                if provider == 'openai':
                    result=await media.generate_openai(app.state.gemini_client,item,model,key,cfg['image_quality'])
                elif provider=='cloudflare':
                    result=await media.generate_cloudflare(app.state.gemini_client,item,model,key,cfg['cloudflare_account_id'])
                else: result=await media.generate(app.state.gemini_client,item,model,key)
                await usage.finish(request_id,'success')
                return result
            except Exception as error:
                await usage.finish(request_id,'failed',code=getattr(getattr(error,'response',None),'status_code',type(error).__name__))
                log.warning('Image generation %s failed: %s',provider,type(error).__name__)
                errors.append(provider+':'+type(error).__name__)
    result=await local_image(item,cfg)
    if errors: result['image_error']='Использован шаблон: '+'; '.join(errors)
    return result

async def local_image(item,cfg):
    tag=channel_tag(route_of(item),cfg)
    logo=await setting(route_key('logo',route_of(item)))
    if not isinstance(logo,bytes):logo=None
    result=await asyncio.to_thread(templates.create,item,tag,cfg.get('template_style','auto'),logo)
    result['image_tag']=tag
    return result

async def prepare_item(ref, item, cfg):
    started = time.monotonic()
    context = item.get('summary','')
    if channel_kind(route_of(item)) == 'events': context += '\nОфіційні дані події: '+json.dumps(item.get('event',{}),default=str,ensure_ascii=False)
    local=local_editor.prepare(item,cfg)
    lang=local['source_language']
    original=cfg['text_mode']=='original' or (lang=='uk' and cfg['language'].startswith('українська')) or cfg['cost_mode']=='off'
    if not policy.topic_allowed(dict(local,text=local.get('text','')+' '+context),cfg):
        result=dict(local,status='filtered',reason='Тема, регион или ключевые слова отключены')
        await asyncio.to_thread(ref.update,result);return result
    ai=None
    cache_key=hashlib.sha256(json.dumps({'title':item['title'],'context':context,'language':cfg['language'],
        'length':cfg['body_length'],'tone':cfg['tone'],'category':item['category'],'review':cfg['review_provider'],
        'models':{p:{'primary':cfg[p+'_model'],'backup':cfg[p+'_backup_model']} for p in policy.PROVIDERS},'translation_order':cfg['translation_order']},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    cache=db.collection('translation_cache').document(cache_key)
    if not original:
        cached=await asyncio.to_thread(cache.get)
        if cached.exists: ai=cached.to_dict().get('result');local['text_method']='cache'
        else:
            ai = await asyncio.wait_for(ai_prepare(item['title'], context, item['url'], item['category'], cfg),80)
            if ai:
                await asyncio.to_thread(cache.set,{'result':ai,'created_at':now()})
                local['text_method']='translated'
    if ai is None:
        ai={'title':local['title'],'body':local['text'] or local['title'],'category':item['category'],
            'sensitive':local['sensitive'],'has_missing_event_details':False,'review_reason':local['review_reason'],
            'is_event':True}
    else: local['translation_missing']=False
    if route_of(item) == 'news' and ai['category'] == 'events' and ai.get('is_event',True) is True:
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
                   category=ai['category'], sensitive=ai['sensitive'] or item.get('manual_review',False),
                   missing_details=ai['has_missing_event_details'], review_passed=ai.get('review_passed',False),
                   ai_provider=ai.get('ai_provider',''), ai_model=ai.get('ai_model',''),
                   review_reason=ai.get('review_reason',''), review_provider=ai.get('review_provider','off'))
    updated.update(source_language=lang,text_method=local['text_method'],translation_missing=local.get('translation_missing',False))
    updated.update(topic=sanitize(ai.get('topic',''),45),title_fi=sanitize(ai.get('title_fi',''),180),
                   text_fi=sanitize(ai.get('body_fi',''),cfg['body_length']//2))
    if channel_kind(route_of(item)) == 'events':
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
            updated.update(await local_image(updated,cfg))
            updated['image_error'] = 'Время подбора фото истекло; создан бесплатный шаблон'
        updated['status'] = 'pending'
    else:
        updated['status'] = 'filtered'
    updated.update(prepared_at=now(), reason='Отключённая тема, город или ключевые слова' if updated['status']=='filtered' else '')
    await asyncio.to_thread(ref.update, updated)
    return updated

async def show_failed(chat):
    await observability.show(this(),chat)
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
    if not snap.exists or snap.to_dict().get('status') not in publication.MANUAL_STATUSES: return await item_status(chat,docid)
    item = snap.to_dict()
    cfg = dict(await editor_config(route_of(item)),require_image=False)
    if not item.get('image_bytes') and not item.get('image_file_id'):
        try: image = await asyncio.wait_for(prepare_image(item,cfg),90)
        except asyncio.TimeoutError: image={'image_error':'Подбор изображения занял слишком долго. Можно повторить или отправить текст.'}
        if image:
            await asyncio.to_thread(ref.update,image)
            item.update(image)
    if cfg['require_image'] and not (item.get('image_bytes') or item.get('image_file_id')):
        return await say(chat,'Изображение недоступно. Проверьте ключ, модель и дневной лимит генераций.')
    sent = await send_item(chat,publication.draft(item,route_of(item)),cfg)
    if sent.get('photo'):
        await asyncio.to_thread(ref.update, {'image_file_id':sent['photo'][-1]['file_id'],
                                            'image_kind':sent.get('_image_kind',item.get('image_kind','')),'image_tag':sent.get('_image_tag','')})
    return await say(chat, 'Это превью в личном чате. В канал ничего не отправлено.')

async def collect(route=None):
    route = route or ROUTE.get()
    if not CHANNELS.get(route,{}).get('enabled',True):return {'disabled':True}
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
        if result.get('errors'):await observability.alert(this(),route,'collection-errors',f'⚠️ {target_channel(route)}: ошибок за сбор {result["errors"]}. Откройте «Диагностика» и «Источники».')
        return result
    except asyncio.TimeoutError:
        result=dict(await setting(route_key('last_collect'),{}),timeout=True,at=now().isoformat(),message='Обработка продолжится в следующем запуске.')
        await put_setting(route_key('last_collect'),result)
        return result
    except Exception as error:
        await observability.error(this(),'Сборщик',route,type(error).__name__,recommendation='Проверьте источники и Firestore; запустите сбор повторно.',notify=True)
        await put_setting(route_key('last_collect'),{'error':type(error).__name__,'at':now().isoformat()})
        return {'error':type(error).__name__}
    finally:
        if acquired: await asyncio.to_thread(release,db.transaction())
        ROUTE.reset(context)

async def prepare_backlog(cfg, stats, started, attempted_ids=None):
    attempted_ids = attempted_ids if attempted_ids is not None else set()
    route = ROUTE.get()
    retries = (await item_docs('awaiting_ai',limit=10000))+(await item_docs('processing',limit=10000))
    queued = await item_docs('queued',limit=10000)
    if channel_kind(route) != 'events': queued.reverse()  # Fresh news must not wait behind days of expired backlog.
    else: queued.sort(key=lambda d:d.to_dict().get('event',{}).get('start') or now()+timedelta(days=999))
    queued.sort(key=lambda d:policy.priority(d.to_dict(),cfg),reverse=True)
    candidates = [d for d in retries if d.to_dict().get('attempts',0)<3][:1]+queued
    deadline = min(started+210,time.monotonic()+90)
    for doc in candidates:
        if stats['attempted']>=cfg['processing_batch_size'] or time.monotonic()>deadline-2: break
        if doc.id in attempted_ids: continue
        item = doc.to_dict()
        if policy.event_expired(item,now()) or (channel_kind(route)!='events' and (now()-(item.get('source_published_at') or item.get('created_at',now()))).total_seconds()>cfg['max_age_hours']*3600):
            if stats['expired']<30:
                await asyncio.to_thread(doc.reference.update,{'status':'expired','reason':'Материал устарел для автоматической публикации'})
                stats['expired']+=1
            continue
        if item.get('source_id'):
            source = await asyncio.to_thread(db.collection('sources').document(item['source_id']).get)
            if not source.exists or not source.to_dict().get('enabled',True):
                await asyncio.to_thread(doc.reference.update,{'status':'filtered','reason':'Источник отключён'})
                continue
        tries = item.get('attempts',0)+1
        attempted_ids.add(doc.id)
        stats['attempted']+=1
        await asyncio.to_thread(doc.reference.update,{'status':'processing','attempts':tries})
        item['attempts']=tries
        try:
            updated = await asyncio.wait_for(prepare_item(doc.reference,item,cfg),timeout=max(1,min(100,deadline-time.monotonic())))
            stats['filtered' if updated['status']=='filtered' else 'prepared']+=1
            if stats['published']<cfg['batch_size'] and (await setting(route_key('autopublish'),False)) and updated['status']=='pending' and await publish(doc.id,automatic=True,cfg=cfg):
                stats['published']+=1
        except Exception as error:
            await transition_item(doc.reference,{'status':'awaiting_ai','reason':type(error).__name__,'attempts':tries},('processing','awaiting_ai','queued'))
            stats['errors']+=1

async def collect_batch():
    cfg = await editor_config()
    route = ROUTE.get()
    auto = await setting(route_key('autopublish'),False)
    started = time.monotonic()
    stats = {'seen':0,'new':0,'prepared':0,'published':0,'errors':0,'filtered':0,'attempted':0,'expired':0,'duplicates':0,'skipped_old':0}
    stats['scheduled_published']=await jobs.run_due(this(),route)
    if auto:
        # Read lightweight metadata from the whole ready queue. Blocked early records cannot starve later drafts.
        pending = await item_docs('pending',limit=100000)
        ledger=(await asyncio.to_thread(db.collection('settings').document(route_key('publication_ledger')).get)).to_dict() or {}
        for doc in sorted(reversed(pending),key=lambda d:policy.priority(d.to_dict(),cfg),reverse=True):
            value = dict(doc.to_dict(),_has_image=bool(doc.to_dict().get('image_kind') or doc.to_dict().get('image_file_id')))
            if time.monotonic()-started>60 or stats['published']>=cfg['batch_size']: break
            if policy.automatic_reason(value,cfg,now(),ledger): continue
            if await publish(doc.id,automatic=True,cfg=cfg):
                stats['published']+=1
                ledger=(await asyncio.to_thread(db.collection('settings').document(route_key('publication_ledger')).get)).to_dict() or {}
    # Existing work is processed BEFORE feed intake; a slow feed cannot consume all AI preparation time.
    attempted_ids=set()
    await prepare_backlog(cfg,stats,started,attempted_ids)
    await put_setting(route_key('last_collect'),dict(stats,phase='existing_queue',at=now().isoformat()))
    docs = await documents(db.collection('sources'))
    if not docs:
        await seed_catalog()
        docs = await documents(db.collection('sources'))
    active = [d for d in docs if route_of(d.to_dict())==route and d.to_dict().get('enabled',True)
              and d.to_dict().get('kind')!='manual' and d.to_dict().get('category','news') in cfg['categories']]
    offset = int(await setting(route_key('source_cursor'),0) or 0)%max(1,len(active))
    active = active[offset:]+active[:offset]
    await put_setting(route_key('source_cursor'),offset+1)
    sem = asyncio.Semaphore(6)
    async def read(doc):
        async with sem:
            try:
                source=doc.to_dict()
                if source.get('last_ok') and (now()-source['last_ok']).total_seconds()<int(source.get('check_minutes',15))*60:return doc,[],{}
                rows,progress = await sources.fetch(source,cfg,now())
                await asyncio.to_thread(doc.reference.update,dict(last_ok=now(),last_error='',last_count=len(rows),found_count=doc.to_dict().get('found_count',0)+len(rows)))
                return doc,rows,progress
            except Exception as error:
                stats['errors']+=1
                code=getattr(getattr(error,'response',None),'status_code',type(error).__name__)
                await observability.error(this(),'Источник',route,code,source_id=doc.id,recommendation='Проверьте адрес RSS/API, доступность сайта и интервал проверки.')
                await asyncio.to_thread(doc.reference.update,{'last_error':str(code),'checked_at':now()})
                return doc,[],{}
    tasks=[asyncio.create_task(read(d)) for d in active]
    batches=[]
    if tasks:
        try:
            done,pending=await asyncio.wait(tasks,timeout=max(1,min(65,195-(time.monotonic()-started))))
            stats['sources_timed_out']=len(pending)
            for task in pending: task.cancel()
            await asyncio.gather(*pending,return_exceptions=True)
            batches=[task.result() for task in tasks if task in done]
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
    recent=[(d.id,d.to_dict()) for d in await item_metadata() if route_of(d.to_dict())==route and (now()-d.to_dict().get('created_at',now())).total_seconds()<172800]
    async def ingest(doc,raw):
        source=doc.to_dict();stats['seen']+=1
        if not valid_url(raw.get('url','')) or not raw.get('title'): return
        item=dict(raw,status='queued',text='',route=route,source_id=doc.id,source_name=source.get('name',doc.id),
                  source_images=source.get('images_allowed',False),source_language=source.get('language',''),category=source.get('category','news'),created_at=now(),attempts=0)
        if policy.event_expired(item,now()) or item.get('event',{}).get('cancelled') or (channel_kind(route)!='events' and item.get('source_published_at') and (now()-item['source_published_at']).total_seconds()>cfg['max_age_hours']*3600):
            item.update(status='expired',reason='Истёк срок актуальности или событие отменено');stats['skipped_old']+=1
        ref=db.collection('items').document(sources.identity(item))
        if (await asyncio.to_thread(ref.get)).exists:
            await asyncio.to_thread(ref.update,{'last_seen_at':now()});stats['duplicates']+=1;return
        item['content_hash']=local_editor.fingerprint(item)
        duplicate=next(((identity,value) for identity,value in recent if value.get('status')!='duplicate' and (value.get('content_hash')==item['content_hash'] or local_editor.same_story(item,value))),None)
        if duplicate:
            item.update(status='duplicate',original_id=duplicate[0],reason='Похожий материал уже найден: '+duplicate[0]);stats['duplicates']+=1
        recent.append((ref.id,item))
        try:
            await asyncio.to_thread(ref.create,item);stats['new']+=1
        except Exception as error:
            if type(error).__name__!='AlreadyExists': raise
            stats['duplicates']+=1
    processed_rows=0
    for index in range(max([len(rows) for _,rows,_ in batches]+[0])):
        if time.monotonic()-started>210: break
        await asyncio.gather(*(ingest(doc,rows[index]) for doc,rows,_ in batches if index<len(rows)))
        processed_rows=index+1
    for doc,rows,progress in batches:
        # Never advance past a page whose remaining records were not stored.
        if progress and len(rows)<=processed_rows: await asyncio.to_thread(doc.reference.update,progress)
    await put_setting(route_key('last_collect'),dict(stats,phase='intake',at=now().isoformat()))
    if stats['attempted']<cfg['processing_batch_size'] and time.monotonic()-started<200:
        await prepare_backlog(cfg,stats,started,attempted_ids)
    return stats

async def select_context(chat, user, requested=None):
    route = requested or await selected_route(chat)
    if route not in CHANNELS: return False
    if route != 'news' and not await admin_access(user,route):
        await say(chat,f'Нужны ваши права публикации в {target_channel(route)}.')
        return False
    ROUTE.set(route)
    if requested: await put_setting(f'route:{chat}',route)
    return True

async def authorize_item(user, docid):
    if '/' in docid or len(docid)>100: return False
    snap = await asyncio.to_thread(db.collection('items').document(docid).get)
    if not snap.exists: return False
    return route_of(snap.to_dict()) == 'news' or await admin_access(user,route_of(snap.to_dict()))

async def authorize_source(user, source_id):
    if '/' in source_id or len(source_id)>100: return False
    snap = await asyncio.to_thread(db.collection('sources').document(source_id).get)
    return snap.exists and (route_of(snap.to_dict()) == 'news' or await admin_access(user,route_of(snap.to_dict())))

async def request_collection(chat):
    if ROUTE.get() not in ('news','events'): await put_setting('requested_channel',ROUTE.get())
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
    connected = await ai_router.available()
    for provider in cfg['ai_order']:
        if time.monotonic()-started>22: break
        if not usage.allowed(provider,cfg[provider+'_model'],cfg) or not connected.get(provider) or ai_router.cooldown(provider,cfg[provider+'_model']): continue
        try:
            response = await asyncio.wait_for(ai_router.execute(app.state.gemini_client,provider,cfg[provider+'_model'],facts,cfg,'assistant'),min(cfg['ai_timeout'],8))
            if isinstance(response.get('answer'),str): return await say(chat,provider+': '+response['answer'])
        except Exception as error: ai_router.mark_failure(provider,cfg[provider+'_model'],error)
    return await say(chat,'ИИ сейчас недоступны. Проверьте ключи, модели и баланс: /settings → ИИ.')

async def _handle_message(message):
    user=message.get('from',{}).get('id')
    chat_info=message.get('chat',{})
    if chat_info.get('type')!='private': return
    chat=chat_info['id']
    text=(message.get('text') or message.get('caption') or '').strip()
    if not await admin_access(user):
        if credentials.looks_like_key(text): await erase_key_message(message)
        if text.startswith('/start') or text=='/help':
            await say(chat, access_hint())
        return
    UI_ACTIVE.set(True)
    text, requested = controls.command(text)
    if text == '/cancel':
        await put_setting(f'input:{user}',None)
        await put_setting(f'edit_state:{user}',None)
        await put_setting(f'import:{user}',None)
        await put_setting(f'manual_send:{user}',None)
        await put_setting(f'reconcile:{user}',None)
        await put_setting(f'dialog:{user}',None)
        if not await select_context(chat,user): return
        return await say(chat,'Ввод отменён. Выберите действие в меню.')
    if not text.startswith('/'):
        state = await setting(f'input:{user}')
        if isinstance(state,dict) and await consume_input(message,state): return
    dialog_state=await setting(f'dialog:{user}') if not text.startswith('/') else None
    if isinstance(dialog_state,dict) and dialog_state.get('key')=='cloudflare_account_id' and re.fullmatch('[a-fA-F0-9]{32}',text):
        return await workflows.consume(this(),chat,user,message,text)
    if credentials.looks_like_key(text) or (not text.startswith('/') and re.fullmatch(r'[A-Za-z0-9_.-]{24,}',text)):
        erased = await erase_key_message(message)
        return await say(chat,'Похоже на API-ключ. Он не сохранён и не отправлен ИИ. Сначала выберите /ai → провайдер → Добавить ключ.'+
                         (' Удалите сообщение с ключом вручную.' if not erased else ''))
    if text.startswith('/'):
        await put_setting(f'dialog:{user}',None)
        await put_setting(f'input:{user}',None)
        await put_setting(f'import:{user}',None)
        await put_setting(f'edit_state:{user}',None)
    if text.startswith('/channel '):
        choice = text.split(maxsplit=1)[1]
        if choice not in CHANNELS: return await say(chat,'/channel news или /channel events')
        if await select_context(chat,user,choice): return await dashboard(chat)
        return
    if not await select_context(chat,user,requested): return
    if not text.startswith('/') and await workflows.consume(this(),chat,user,message,text): return
    await asyncio.to_thread(db.collection('admin_chats').document(str(user)).set,{'user':user,'last_seen':now()})
    if text == '/channels': return await channels.show(this(),chat,user)
    if text == '/usage': return await workflows.usage_report(this(),chat)
    if text == '/backup': return await workflows.backup(this(),chat,user)
    if text == '/search': return await workflows.dialog(this(),chat,user,'search')
    if text in ('/import','/newpost'): return await start_import(chat,user)
    if text.startswith('/import '): return await import_material(chat,user,message,text[8:].strip())
    if not text.startswith('/'):
        importing=await setting(f'import:{user}')
        if isinstance(importing,dict):
            if importing.get('expires',now())<=now():
                await put_setting(f'import:{user}',None)
                return await say(chat,'Время добавления истекло. Нажмите «Добавить» ещё раз.')
            if not await select_context(chat,user,importing.get('route')): return
            return await import_material(chat,user,message,text)
        if message.get('forward_origin'): return await import_material(chat,user,message,text)
    if text in ('/start','/menu'): return await dashboard(chat)
    if text == '/ai': return await show_ai(chat)
    if text == '/mass': return await mass.menu(this(),chat)
    if text == '/unpublished': return await show_unpublished(chat)
    if text == '/archive': return await show_unpublished(chat,'published')
    if text == '/diagnose': return await show_diagnosis(chat)
    if text == '/images': return await show_settings(chat,'images')
    if text.startswith('/status '):
        docid = text.split(maxsplit=1)[1]
        if not await authorize_item(user,docid): return await say(chat,'Материал не найден или нет доступа.')
        return await item_status(chat,docid)
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
                     date_only=len(parts[1])==10,is_free=parts[4].casefold() in ('0','бесплатно','безкоштовно','maksuton','free'))
        if await transition_item(ref,{'event':event,'missing_details':False,'sensitive':True,'review_passed':False},
                                 tuple(s for s in publication.MANUAL_STATUSES if s!='published')):
            return await say(chat,'Данные сохранены. Проверьте превью, затем публикуйте кнопкой.')
        return await say(chat,'Данные можно менять только у неотправленных материалов.')
    edit_key=f'edit_state:{user}'
    state=await setting(edit_key) if text and not text.startswith('/') else None
    if state and await authorize_item(user,state):
        ref=db.collection('items').document(state)
        if await transition_item(ref, {'text':sanitize(text,2500),'review_passed':False,'title_fi':'','text_fi':'','status':'pending',
                                       'sensitive':True,'import_error':'','review_reason':'Ручная правка; нужна ручная публикация'},
                                 tuple(s for s in publication.MANUAL_STATUSES if s!='published')):
            await put_setting(edit_key,None)
            return await say(chat,'✏️ Текст сохранён. Откройте /unpublished и выберите канал для публикации.')
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
        return await retry_item(chat,user,text.split(maxsplit=1)[1])
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
            source=await asyncio.to_thread(ref.get)
            if not source.exists: return await say(chat,'Источник не найден')
            if source.to_dict().get('kind')=='manual': return await say(chat,'Это справочник для ручного импорта. Нажмите «Добавить» и пришлите конкретный анонс.')
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

async def retry_item(chat,user,docid):
    if not await authorize_item(user,docid): return await say(chat,'Нет доступа к материалу.')
    ref=db.collection('items').document(docid)
    if not await transition_item(ref,{'status':'awaiting_ai','attempts':0,'reason':''},tuple(s for s in publication.MANUAL_STATUSES if s!='published')):
        return await item_status(chat,docid)
    return await say(chat,'Обработка ИИ поставлена в очередь следующего сбора. Устаревшие материалы можно отправить вручную без повторной обработки.')

async def advanced_callback(chat,user,data):
    if data in ('fastnews','fastnewsconfirm'): await operations.fastnews(this(),chat,user,data=='fastnewsconfirm')
    elif data=='mass': await mass.menu(this(),chat)
    elif data.startswith('masspick:'): await mass.pick(this(),chat,user,int(data[9:]))
    elif data.startswith('masstarget:'): await mass.preview(this(),chat,user,data[11:])
    elif data.startswith('massconfirm:'): await mass.confirm(this(),chat,user,data[12:])
    elif data=='massdiscard': await put_setting(f'mass:{user}',None);await say(chat,'Отменено.')
    elif data.startswith('massview:'): await mass.view(this(),chat,user,int(data[9:]))
    elif data=='massstatus': await mass.status(this(),chat,user)
    elif data.startswith('massstop:'): await mass.cancel(this(),chat,user,data[9:])
    elif data.startswith('massstopok:'): await mass.cancel(this(),chat,user,data[11:],True)
    elif data=='operations': await operations.menu(this(),chat)
    elif data.startswith('op:'): await operations.prepare(this(),chat,user,data[3:])
    elif data.startswith('assignsource:'): await operations.assign(this(),chat,user,data[13:])
    elif data.startswith('sourcecopy:'):
        state=await setting(f'sourcecopy:{user}')
        if isinstance(state,dict) and state.get('expires',now())>now():
            await operations.assign(this(),chat,user,state['id'],data[11:])
            await put_setting(f'sourcecopy:{user}',None)
        else: await say(chat,'Выбор источника истёк. Откройте источники снова.')
    elif data=='channels': await channels.show(this(),chat,user)
    elif data.startswith('dialog:'): await workflows.dialog(this(),chat,user,data[7:])
    elif data.startswith('inputsetting:'): await workflows.dialog(this(),chat,user,'setting',data[13:])
    elif data.startswith('errorresolved:'):
        ref=db.collection('errors').document(data[14:]);value=(await asyncio.to_thread(ref.get)).to_dict() or {}
        if value.get('route')==ROUTE.get():await asyncio.to_thread(ref.update,{'status':'resolved','resolved_at':now()})
        await show_failed(chat)
    elif data=='usage': await workflows.usage_report(this(),chat)
    elif data=='browse': await workflows.browse(this(),chat,user)
    elif data.startswith('browsepage:'): await workflows.browse(this(),chat,user,page=int(data[11:]))
    elif data.startswith('browse:'):
        _,field,value=data.split(':');await workflows.browse(this(),chat,user,field,value)
    elif data.startswith('photo:'): await workflows.dialog(this(),chat,user,'photo',data[6:])
    elif data.startswith('schedule:'): await workflows.dialog(this(),chat,user,'schedule',data[9:])
    elif data.startswith('select:'): await jobs.select(this(),chat,user,data[7:])
    elif data=='bulkmenu': await jobs.show(this(),chat,user)
    elif data=='bulkclear':
        await put_setting(f'selection:{user}',[]);await jobs.show(this(),chat,user)
    elif data.startswith('bulk:'):
        _,action,target=data.split(':');await jobs.prepare_bulk(this(),chat,user,action,target)
    elif data.startswith('bulkconfirm:'): await jobs.confirm_bulk(this(),chat,user,data[12:])
    elif data=='bulkcancel': await put_setting(f'bulk:{user}',None);await say(chat,'Групповое действие отменено.')
    elif data=='backup': await workflows.backup(this(),chat,user)
    elif data.startswith('restore:'): await workflows.restore_backup(this(),chat,user,data[8:])
    elif data.startswith('restoreok:'): await workflows.restore_backup(this(),chat,user,data[10:],True)
    elif data.startswith(('chremove:','chremoveok:','chrestore:')):
        action,route=data.split(':')
        if not await key_access(user): await say(chat,'Каналы архивирует владелец основного канала.');return True
        if route not in CHANNELS or route in ('news','events'):return True
        if action=='chremove':
            await say(chat,'Отключить '+target_channel(route)+'? Настройки и история сохранятся; сбор и публикации остановятся.',[
                [{'text':'Подтверждаю отключение','callback_data':'chremoveok:'+route}],[{'text':'Отмена','callback_data':'channels'}]])
        else:
            await asyncio.to_thread(db.collection('channels').document(route).update,{'enabled':action=='chrestore'})
            await channels.load(this());await channels.show(this(),chat,user)
    elif data in ('mode:off','mode:free'):
        mode=data[5:];current=await editor_config()
        await put_setting(route_key('previous_processing_mode'),{k:current[k] for k in ('cost_mode','text_mode','image_mode','review_provider','ai_order')})
        await asyncio.to_thread(db.collection('settings').document(route_key('editor')).set,
            {'value':{'cost_mode':mode,'text_mode':'original' if mode=='off' else 'auto','image_mode':'source_template',
                      'review_provider':'off','ai_order':['openrouter','cloudflare']+[p for p in current['ai_order'] if p not in ('openrouter','cloudflare')]}},merge=True)
        await say(chat,'Сохранено: '+('без ИИ, текст на языке оригинала, оригинальное фото или шаблон.' if mode=='off' else
                                    'бесплатный режим. Украинский текст — без ИИ; иностранный — через разрешённый бесплатный API, иначе на ручную проверку.'))
        await dashboard(chat)
    elif data.startswith('sourcefrequency:'): await workflows.dialog(this(),chat,user,'sourcefrequency',data[16:])
    elif data.startswith('checksource:'):
        sid=data[12:]
        if not await authorize_source(user,sid):await say(chat,'Нет доступа к источнику.');return True
        ref=db.collection('sources').document(sid);source=(await asyncio.to_thread(ref.get)).to_dict()
        try:
            rows,_=await sources.fetch(source,await editor_config(),now())
            await asyncio.to_thread(ref.update,{'last_ok':now(),'last_error':'','last_count':len(rows)})
            await say(chat,f'Источник отвечает. Найдено на проверенной странице: {len(rows)}. Для сохранения запустите сбор.')
        except Exception as error:
            code=getattr(getattr(error,'response',None),'status_code',type(error).__name__)
            await asyncio.to_thread(ref.update,{'last_error':str(code),'checked_at':now()})
            await say(chat,'Источник не ответил: '+str(code)+'. Проверьте адрес и доступность RSS/API.')
    elif data=='checktelegram':
        me=await tg('getMe',{});member=await tg('getChatMember',{'chat_id':target_channel(ROUTE.get()),'user_id':me['id']})
        hook=await tg('getWebhookInfo',{})
        await say(chat,'Telegram: @'+me['username']+'\nПрава бота: '+str(member.get('can_post_messages',member.get('status')=='creator'))+
                  '\nОжидают webhook: '+str(hook.get('pending_update_count',0))+'\nПоследняя ошибка webhook: '+str(hook.get('last_error_message','нет')))
    else:return False
    return True

async def _handle_callback(callback):
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
            log.info('Callback acknowledgement expired; checking current permissions and state')
        else:
            raise
    if not await admin_access(user):
        await say(chat, access_hint())
        return
    data=callback.get('data','')
    UI_ACTIVE.set(True)
    PANEL_MESSAGE.set(callback.get('message',{}).get('message_id'))
    requested = None
    scoped = data.startswith('ctx:')
    if scoped:
        context, separator, data = data[4:].partition('|')
        if not separator or context not in CHANNELS: return
        requested = context
    if data.startswith('route:'):
        await put_setting(f'input:{user}',None)
        await put_setting(f'dialog:{user}',None)
        if await select_context(chat,user,data[6:]): return await dashboard(chat)
        return
    if any(data.startswith(prefix) for prefix in ('pub:','reject:','edit:','preview:','status:','retry:','reconcile:','reconcileok:','photo:','select:')):
        docid = data.split(':',1)[1]
        if not await authorize_item(user,docid): return await say(chat,'Нет доступа к материалу.')
        snapshot = await asyncio.to_thread(db.collection('items').document(docid).get)
        if not snapshot.exists: return await say(chat,'Материал не найден. Откройте /queue.')
        requested = route_of(snapshot.to_dict())
    if not await select_context(chat,user,requested): return
    await put_setting(f'dialog:{user}',None)
    await put_setting(f'import:{user}',None)
    await put_setting(f'edit_state:{user}',None)
    if not data.startswith('keyconfirm:'):
        await put_setting(f'input:{user}',None)
    if not scoped and (data == 'toggle' or data.startswith(('adjust:','category:','cycle:','source:'))):
        await say(chat,'Это меню старой версии. Открываю актуальные настройки выбранного канала; повторите действие новой кнопкой.')
        return await show_settings(chat)
    if data == 'inputcancel':
        await put_setting(f'input:{user}',None)
        return await show_ai(chat)
    if data == 'ai' or data.startswith(('ai:','aienable:','aifirst:','aimodel:','aitest:','keyadd:','keyremove:','keyconfirm:')):
        return await ai_action(chat,user,data)
    if data.startswith('status:'): return await item_status(chat,data[7:])
    if data.startswith('title:'): return await workflows.dialog(this(),chat,user,'title',data[6:])
    if await advanced_callback(chat,user,data): return
    if data == 'import': return await start_import(chat,user)
    if data.startswith('retry:'): return await retry_item(chat,user,data[6:])
    if data.startswith('reconcile:'): return await reconcile_send(chat,user,data[10:])
    if data.startswith('reconcileok:'): return await reconcile_send(chat,user,data[12:],True)
    if data.startswith('unpublished:'):
        _,state,page = data.split(':')
        return await show_unpublished(chat,state,int(page))
    if data == 'diagnose': return await show_diagnosis(chat)
    if data.startswith('destination:'):
        _,target,docid = data.split(':')
        if not await authorize_item(user,docid): return await say(chat,'Нет доступа к материалу.')
        return await prepare_manual_send(chat,user,docid,target)
    if data.startswith('sendconfirm:'): return await confirm_manual_send(chat,user,data[12:])
    if data == 'sendcancel':
        await put_setting(f'manual_send:{user}',None)
        return await say(chat,'Публикация отменена. Материал сохранён.')
    if data=='home': return await dashboard(chat)
    if data.startswith('section:'): return await show_settings(chat,data[8:])
    if data.startswith('sources:'): return await show_sources(chat,int(data[8:]))
    if data.startswith('queue:'): return await show_queue(chat,int(data[6:]))
    if data.startswith('number:'):
        _,key,value = data.split(':')
        if key not in policy.RANGES: return
        await change_config(key,value)
        section = next(k for k,(_,keys) in SECTIONS.items() if key in keys)
        return await show_settings(chat,section)
    if data=='settings': return await show_settings(chat)
    if data.startswith('category:'):
        _,key,value = data.split(':')
        if key not in CATEGORIES or value not in ('0','1'): return
        cfg = await editor_config()
        categories = [k for k in cfg['categories'] if k != key]
        if value == '1': categories.append(key)
        await change_config('categories',categories)
        return await show_settings(chat,'topics')
    if data.startswith('value:'):
        _,key,index = data.split(':')
        if key not in policy.CHOICES and type(policy.DEFAULTS.get(key)) is not bool: return
        choices = policy.CHOICES.get(key,(False,True))
        if not index.isdigit() or int(index)>=len(choices): return
        await change_config(key,choices[int(index)])
        section = next(k for k,(_,keys) in SECTIONS.items() if key in keys)
        return await show_settings(chat,'ai_options' if section=='ai' else section)
    if data.startswith('source:'):
        _,sid,value = data.split(':')
        if value not in ('0','1'): return
        if not await authorize_source(user,sid): return await say(chat,'Источник не найден или нет доступа.')
        ref = db.collection('sources').document(sid)
        snap = await asyncio.to_thread(ref.get)
        if snap.exists and snap.to_dict().get('kind')=='manual': return await start_import(chat,user)
        if snap.exists: await asyncio.to_thread(ref.update,{'enabled':value=='1','pending_review':False})
        return await show_sources(chat)
    if data.startswith('preview:'): return await preview(chat,data[8:])
    if data=='sources': return await show_sources(chat)
    if data=='queue': return await show_queue(chat)
    if data=='help': return await show_help(chat)
    if data=='stats': return await show_stats(chat)
    if data in ('auto:0','auto:1'):
        await put_setting(route_key('autopublish'),data=='auto:1')
        return await show_settings(chat)
    if data=='collect': return await request_collection(chat)
    if data.startswith('pub:'):
        return await choose_destination(chat,user,data[4:])
    if data.startswith('reject:'):
        ref=db.collection('items').document(data[7:])
        await transition_item(ref, {'status':'rejected','reason':'Отклонено редактором; можно восстановить'},tuple(s for s in publication.MANUAL_STATUSES if s!='published'))
        return await item_status(chat,data[7:])
    if data.startswith('edit:'):
        snapshot = await asyncio.to_thread(db.collection('items').document(data[5:]).get)
        if not snapshot.exists or snapshot.to_dict().get('status') not in tuple(s for s in publication.MANUAL_STATUSES if s!='published'):
            return await item_status(chat,data[5:])
        await put_setting(f'input:{user}',None)
        await put_setting(f'edit_state:{user}',data[5:])
        return await say(chat,'Пришли новый текст одним сообщением. Заголовок и ссылка останутся прежними.')

async def ui_request(handler, value):
    tokens = [(var,var.set(default)) for var,default in ((ROUTE,'news'),(UI_ACTIVE,False),(PANEL_MESSAGE,None))]
    chat = (value.get('message',{}).get('chat',{}) if 'data' in value else value.get('chat',{})).get('id')
    try:
        await channels.load(this())
        return await handler(value)
    finally:
        try:
            if chat is not None: await ensure_keyboard(chat)
        except Exception as error:
            # A menu refresh failure must not replay an already completed key/publication action.
            log.warning('Keyboard refresh failed: %s',type(error).__name__)
        finally:
            for var,token in reversed(tokens): var.reset(token)

async def handle_message(message): return await ui_request(_handle_message,message)
async def handle_callback(callback): return await ui_request(_handle_callback,callback)

@app.get('/health')
async def health(): return {'ok':True,'version':VERSION,'features':['persistent_menu','runtime_ai_keys','publication_status','manual_destinations','publication_diagnostics','event_import','no_ai','local_images','free_mode','channel_registry','scheduled_posts','durable_mass_publication','story_uniqueness','publication_worker','ai_error_details','priority_news']}

@app.post('/admin/publication-status')
async def publication_status_endpoint(request:Request):
    if not secrets.compare_digest(request.headers.get('X-Cron-Secret',''),CRON_SECRET): raise HTTPException(403)
    return {route:await publication_report(route) for route in CHANNELS}

@app.post('/admin/ai-status')
async def ai_status_endpoint(request:Request):
    if not secrets.compare_digest(request.headers.get('X-Cron-Secret',''),CRON_SECRET): raise HTTPException(403)
    summary = {}
    for provider in policy.PROVIDERS:
        info = await credentials.describe(provider)
        summary[provider] = {'configured':info['configured'],'source':info['mode']}
    return {'key_management':os.getenv('AI_KEY_MANAGEMENT')=='1','providers':summary}

@app.post('/admin/self-test')
async def self_test_endpoint(request:Request):
    if not secrets.compare_digest(request.headers.get('X-Cron-Secret',''),CRON_SECRET):raise HTTPException(403)
    result=await asyncio.to_thread(templates.create,{'title':'Перевірка шаблону','category':'news'},'#SuomiNewsUA')
    return {'ok':bool(result.get('image_bytes')),'version':VERSION,'template_bytes':len(result['image_bytes']),
            'ai_requests':0,'telegram_publications':0}

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
    return await collect_slot('news')

@app.post('/collect/events')
async def collect_events_endpoint(request:Request):
    if not secrets.compare_digest(request.headers.get('X-Cron-Secret',''),CRON_SECRET): raise HTTPException(403)
    return await collect_slot('events')

async def collect_slot(base):
    await channels.load(this())
    extras=[r for r,v in CHANNELS.items() if r not in ('news','events') and v.get('enabled',True)]
    if not extras:return await collect(base)
    cursor=int(await setting('extra_channel_cursor',0) or 0)
    requested=await setting('requested_channel')
    route=requested if requested in extras else extras[cursor%len(extras)]
    await put_setting('extra_channel_cursor',cursor+1)
    if requested==route:await put_setting('requested_channel',None)
    results=await asyncio.gather(collect(base),collect(route),return_exceptions=True)
    return {r:(v if isinstance(v,dict) else {'error':type(v).__name__}) for r,v in zip((base,route),results)}

@app.post('/admin/migrate')
async def migrate_endpoint(request:Request):
    if not secrets.compare_digest(request.headers.get('X-Cron-Secret',''),CRON_SECRET): raise HTTPException(403)
    added = await seed_catalog()
    for sid,value in CATALOG.items():
        if value.get('language'):
            await asyncio.to_thread(db.collection('sources').document(sid).set,{'language':value['language']},merge=True)
    # Backfill routes for legacy items; retain IDs and status to avoid reposting old material.
    updated = 0
    story_locks_added=0
    for doc in await item_metadata():
        item = doc.to_dict()
        for target,entry in delivery_history(item).items():
            published_at=entry.get('published_at') or item.get('published_at')
            if not isinstance(published_at,datetime):continue
            cfg=await editor_config(target)
            if published_at+timedelta(hours=cfg['dedupe_hours'])<=now():continue
            for identity in uniqueness.keys(item,target):
                lock=db.collection('story_locks').document(identity)
                try:
                    await asyncio.to_thread(lock.create,uniqueness.reservation(doc.id,target,published_at,cfg['dedupe_hours'],'published'))
                    story_locks_added+=1
                except Exception as error:
                    if type(error).__name__!='AlreadyExists':raise
        if not item.get('route'):
            item['route'] = route_of(item)
            await asyncio.to_thread(doc.reference.update,{'route':item['route']})
            # Aliases prevent a different source from reintroducing an old URL after migration.
            if item.get('url'):
                alias = db.collection('items').document(sources.identity(item))
                if alias.id != doc.id and not (await asyncio.to_thread(alias.get)).exists:
                    await asyncio.to_thread(alias.create,{'route':item['route'],'status':'duplicate','url':item['url'],'original_id':doc.id,'created_at':item.get('created_at',now())})
            updated += 1
    return {'ok':True,'sources_added':added,'legacy_items_updated':updated,'story_locks_added':story_locks_added}

@app.post('/admin/watchdog')
async def watchdog_endpoint(request:Request):
    if not secrets.compare_digest(request.headers.get('X-Cron-Secret',''),CRON_SECRET):raise HTTPException(403)
    await channels.load(this())
    return await operations.watchdog(this())

@app.post('/admin/dispatch')
async def dispatch_endpoint(request:Request):
    if not secrets.compare_digest(request.headers.get('X-Cron-Secret',''),CRON_SECRET):raise HTTPException(403)
    await channels.load(this())
    return await dispatch.endpoint(this())

# QUEUE_RECOVERY_BOOTSTRAP
import sys as _recovery_sys
from . import queue_recovery as _queue_recovery
_queue_recovery.install(_recovery_sys.modules[__name__])
