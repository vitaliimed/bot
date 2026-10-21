"""Finland News Bot: Telegram webhook + scheduled RSS collector, Firestore storage."""
import os, re, json, html, hashlib, logging, secrets, asyncio, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
import feedparser
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, HTTPException
from google.cloud import firestore
from .access import can_manage
from . import ai as ai_router, media, policy

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
WEBHOOK_SECRET = os.environ['WEBHOOK_SECRET']
CRON_SECRET = os.environ['CRON_SECRET']
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite')
db = firestore.Client()
TG = f'https://api.telegram.org/bot{BOT_TOKEN}'
DEFAULT_FEEDS = {
    'yle-news-en': ('Yle News English', 'https://yle.fi/rss/news', 'news'),
    'yle-uk': ('Yle Ukrainian', 'https://yle.fi/rss/novyny', 'ukraine'),
    'yle-sports': ('Yle Sport', 'https://yle.fi/rss/urheilu', 'sport'),
}
CATEGORIES = {'news':'#НовиниФінляндії', 'ukraine':'#УкраїнціУФінляндії', 'sport':'#Спорт', 'children':'#Дітям', 'events':'#Події', 'transport':'#Транспорт', 'other':'#Фінляндія'}

def now(): return datetime.now(timezone.utc)
async def documents(query):
    return await asyncio.to_thread(lambda: list(query.stream()))

async def setting(key, default=None):
    d = await asyncio.to_thread(db.collection('settings').document(key).get)
    return d.to_dict().get('value', default) if d.exists else default

async def put_setting(key, value):
    await asyncio.to_thread(db.collection('settings').document(key).set, {'value':value})

async def editor_config():
    stored = await setting('editor', {})
    if not isinstance(stored,dict): stored = {}
    cfg = policy.config(stored)
    if 'gemini_model' not in stored: cfg['gemini_model'] = GEMINI_MODEL
    return cfg

async def change_config(key, value):
    value = policy.validate(key, value)
    # Field updates preserve simultaneous changes by different administrators.
    await asyncio.to_thread(db.collection('settings').document('editor').set,
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

async def say(chat, text, keyboard=None):
    payload = {'chat_id':chat, 'text':fit(text,4096), 'disable_web_page_preview':True}
    if keyboard: payload['reply_markup'] = {'inline_keyboard':keyboard}
    return await tg('sendMessage', payload)

async def admin_access(user_id):
    return await can_manage(user_id, mode=ADMIN_AUTH_MODE, admin_id=ADMIN_ID,
                            channel_id=CHANNEL_ID, telegram=tg)

def access_hint():
    if ADMIN_AUTH_MODE == 'channel_admins':
        return f'Управление доступно владельцу и администраторам канала {CHANNEL_ID} с правом публикации.'
    return 'Управление этим ботом доступно только назначенному администратору.'

def menu():
    return [[{'text':'📰 Очередь','callback_data':'queue'}, {'text':'📡 Источники','callback_data':'sources'}],
            [{'text':'⚙️ Настройки','callback_data':'settings'}, {'text':'📊 Статистика','callback_data':'stats'}],
            [{'text':'🔄 Проверить сейчас','callback_data':'collect'}, {'text':'ℹ️ Помощь','callback_data':'help'}]]

async def dashboard(chat):
    auto = await setting('autopublish', False)
    cfg = await editor_config()
    await say(chat, f'🇫🇮 Finland News — ADMIN\nАвтопубликация: {"ВКЛ" if auto else "ВЫКЛ"}\nПроверка: Cloud Scheduler\nЯзык публикаций: {cfg["language"]}\n\nВыберите раздел:', menu())

async def show_settings(chat):
    cfg = await editor_config()
    auto = await setting('autopublish', False)
    providers = ai_router.available()
    lines = ['⚙️ Публикации', f'Автопубликация: {"ВКЛ" if auto else "ВЫКЛ"}',
             f'Время: {cfg["window_start"]}–{cfg["window_end"]}, {cfg["timezone"]}',
             f'Лимит: {cfg["daily_limit"]}/день; интервал: {cfg["interval_minutes"]} мин.',
             f'Изображения: {cfg["image_mode"]}; генераций: до {cfg["image_daily_limit"]}/день',
             'Генераторы: ' + ' → '.join(cfg['image_order']) + '; запросы API оплачиваются отдельно.',
             'ИИ (ключ настроен): ' + ', '.join(f'{k}: {"да" if v else "нет"}' for k, v in providers.items()),
             'Ключи не присылайте боту; подключение — scripts/connect-ai.sh.',
             '\nИзменить: /set ИМЯ значение\nСброс одного параметра: /reset ИМЯ\nСписок текущих значений:']
    lines += [f'{k} = {",".join(map(str,v)) if isinstance(v,list) else v}' for k, v in cfg.items()]
    lines += ['\nПримеры:', '/set daily_limit 8', '/set interval_minutes 90',
              '/set window_start 09:00', '/set exclude_words футбол,хокей',
              '/set review_provider groq', '/set image_daily_limit 2', '/set image_order openai,gemini',
              'Пустой фильтр/подпись: /set exclude_words -',
              'Лимиты и расписание действуют для авто. Кнопка «Опубликовать» — ручное решение.',
              'Чувствительные темы всегда требуют модерации.']
    keyboard = [[{'text': 'Автопубликация: ' + ('ВКЛ' if auto else 'ВЫКЛ'), 'callback_data': 'toggle'}]]
    for key, label in policy.CATEGORY_NAMES.items():
        keyboard.append([{'text': ('✅ ' if key in cfg['categories'] else '⬜ ') + label, 'callback_data': 'category:' + key}])
    keyboard += [[{'text':'🖼 Режим картинок', 'callback_data':'cycle:image_mode'},
                  {'text':'🧑‍💻 Проверяющий ИИ', 'callback_data':'cycle:review_provider'}]]
    await say(chat, '\n'.join(lines), keyboard)

async def show_sources(chat):
    docs = await documents(db.collection('sources'))
    lines = [f'{"🟢" if d.to_dict().get("enabled",True) else "🔴"} {d.id}: {d.to_dict().get("name", "")}' for d in docs[:35]]
    keyboard = [[{'text':('🔴 Отключить ' if d.to_dict().get('enabled',True) else '🟢 Включить ')+d.id,
                  'callback_data':'source:'+d.id}] for d in docs[:20]]
    await say(chat, '📡 Источники:\n'+'\n'.join(lines)+'\n\nДобавить: /addsource Название | https://rss-url | news\nОтключить: /disable ID\nВключить: /enable ID\nУдалить: /deletesource ID\nФото источника разрешены: /sourceimages ID on (или off)\nКатегории: news, ukraine, sport, children, events, transport, other',keyboard)

async def show_queue(chat):
    docs = await documents(db.collection('items').where('status','==','pending').limit(8))
    if not docs: return await say(chat,'Очередь пуста.')
    for d in docs:
        v=d.to_dict()
        keyboard = [[{'text':'✅ Опубликовать','callback_data':'pub:'+d.id},{'text':'❌ Отклонить','callback_data':'reject:'+d.id}],
                    [{'text':'✏️ Редактировать','callback_data':'edit:'+d.id}, {'text':'🖼 Превью','callback_data':'preview:'+d.id}]]
        await say(chat, f'📝 ID: {d.id}\n{v.get("title", "")}\n{v.get("text", "")[:900]}\nИсточник: {v.get("url", "")}\n'
                  f'Изображение: {v.get("image_kind", "нет")}; ошибка: {v.get("image_error", "—")}\nИИ: {v.get("ai_provider", "—")}; проверка: {v.get("review_reason", "—")}', keyboard)

async def show_stats(chat):
    docs = await documents(db.collection('items').limit(500))
    counts = {}
    for d in docs:
        status = d.to_dict().get('status', 'unknown')
        counts[status] = counts.get(status, 0) + 1
    return await say(chat, '📊 Последние до 500 материалов:\n' + '\n'.join(f'{k}: {v}' for k,v in counts.items()))

async def show_help(chat):
    return await say(chat,'Команды:\n/start /sources /queue /stats /settings\n/set ИМЯ значение; /reset ИМЯ\n/addsource Название | https://rss-url | news\n/enable ID /disable ID /deletesource ID\n/auto_on /auto_off\n/collect — ручной сбор\n/retry ID — повторить обработку awaiting_ai\nПубликация и превью через кнопки в /queue. Спорт отключается в /settings.')

async def ai_prepare(title, summary, url, category, cfg=None):
    return await ai_router.prepare(app.state.gemini_client, title, summary, url, category,
                                   cfg or await editor_config(), CATEGORIES)

def fit(text, limit):
    # Telegram counts UTF-16 code units; emoji usually consume two.
    return text.encode('utf-16-le')[:limit * 2].decode('utf-16-le', errors='ignore')

def format_post(item, cfg=None, photo=False):
    cfg = cfg or policy.config()
    limit = 1024 if photo else 4096
    title = fit(item.get('title', 'Новини Фінляндії'), 150 if photo else 300)
    footer = ''
    if item.get('image_kind') == 'generated' and photo: footer += '\n\n🎨 Ілюстрація ШІ'
    elif item.get('image_kind') == 'source' and photo: footer += '\n\n📷 ' + fit(item.get('source_name', 'Джерело новини'), 60)
    if cfg['source_link']:
        url = item.get('url','')
        display = fit(urlparse(url).hostname or '',150) if photo and len(url.encode('utf-16-le'))>800 else url
        footer += '\n\n🔗 Джерело: ' + (display or '')
    if cfg['hashtags']: footer += '\n\n' + CATEGORIES.get(item.get('category'), '#Фінляндія')
    if cfg['signature']: footer += '\n\n' + fit(cfg['signature'],150)
    head = '🇫🇮 ' + title + '\n\n'
    available = limit - len((head + footer).encode('utf-16-le')) // 2
    body = fit(item.get('text', ''), max(0, min(available - 1,cfg['body_length'])))
    if body != item.get('text', ''): body = body.rstrip() + '…'
    return fit(head + body + footer, limit)

async def send_item(chat, item, cfg):
    if cfg['image_mode'] != 'off' and (item.get('image_file_id') or item.get('image_bytes')):
        payload = {'chat_id': chat, 'caption': format_post(item, cfg, photo=True),
                   'disable_notification': cfg['silent']}
        if cfg['source_link'] and len(item.get('url','').encode('utf-16-le')) > 800:
            payload['reply_markup'] = {'inline_keyboard': [[{'text':'🔗 Джерело','url':item['url']}]]}
        if item.get('image_file_id'):
            payload['photo'] = item['image_file_id']
            return await tg('sendPhoto', payload)
        return await tg('sendPhoto', payload, files={'photo': ('news.jpg', item['image_bytes'], 'image/jpeg')})
    if cfg['require_image']:
        raise ValueError('Image required')
    if cfg['silent']:
        return await tg('sendMessage', {'chat_id':chat, 'text':format_post(item, cfg),
                                       'disable_notification':True, 'disable_web_page_preview':True})
    return await say(chat, format_post(item, cfg))

async def publish(docid, automatic=False, cfg=None):
    cfg = cfg or await editor_config()
    ref = db.collection('items').document(docid)
    ledger_ref = db.collection('settings').document('publication_ledger')
    @firestore.transactional
    def claim(tx):
        effective_cfg = cfg
        snap = ref.get(transaction=tx)
        if not snap.exists or snap.to_dict().get('status') != 'pending': return None
        item = snap.to_dict()
        ledger = ledger_ref.get(transaction=tx).to_dict() or {}
        current = now()
        if automatic:
            auto = db.collection('settings').document('autopublish').get(transaction=tx)
            if not auto.exists or not auto.to_dict().get('value',False): return None
            if item.get('source_id'):
                source = db.collection('sources').document(item['source_id']).get(transaction=tx)
                if not source.exists or not source.to_dict().get('enabled',True): return None
            current_settings = db.collection('settings').document('editor').get(transaction=tx)
            latest_cfg = policy.config((current_settings.to_dict() or {}).get('value',{}))
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
        sent = await send_item(CHANNEL_ID, item, cfg)
        update = {'status':'published', 'published_at':now(), 'message_id':sent['message_id']}
        if sent.get('photo'):
            update.update(image_file_id=sent['photo'][-1]['file_id'], image_bytes=firestore.DELETE_FIELD)
        await asyncio.to_thread(ref.update, update)
        return True
    except Exception as error:
        await asyncio.to_thread(ref.update, {'status':'needs_reconciliation', 'error_at':now()})
        log.error('Publish uncertain, needs manual reconciliation: %s (%s)', docid, type(error).__name__)
        return False

async def reserve_image(cfg):
    ref = db.collection('settings').document('image_ledger')
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
    ai = await asyncio.wait_for(ai_prepare(item['title'], item.get('summary',''), item['url'], item['category'], cfg),75)
    if ai is None: raise ValueError('All configured AI writers unavailable')
    updated = dict(item, title=sanitize(ai['title'],300), text=sanitize(ai['body'],cfg['body_length']),
                   category=ai['category'], sensitive=ai['sensitive'],
                   missing_details=ai['has_missing_event_details'], review_passed=ai.get('review_passed',False),
                   ai_provider=ai.get('ai_provider',''), ai_model=ai.get('ai_model',''),
                   review_reason=ai.get('review_reason',''), review_provider=ai.get('review_provider','off'))
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
        docs = await documents(db.collection('items').where('status','==',status).limit(8))
        lines += [f'{d.id}: {status} — {d.to_dict().get("reason", "")}\n{d.to_dict().get("title", "")[:100]}' for d in docs]
    lines += ['/retry ID — повторить awaiting_ai после исправления ключа/модели.',
              'needs_reconciliation: сначала проверьте канал — пост мог уже уйти. Автоматического повтора нет.']
    return await say(chat,'\n'.join(lines))

async def preview(chat, docid):
    ref = db.collection('items').document(docid)
    snap = await asyncio.to_thread(ref.get)
    if not snap.exists or snap.to_dict().get('status') != 'pending': return await say(chat, 'Материал уже обработан.')
    item = snap.to_dict()
    cfg = await editor_config()
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

async def collect():
    # One durable lease across all Cloud Run instances and manual/Scheduler requests.
    lease = db.collection('settings').document('collector_lease')
    token = secrets.token_hex(12)
    @firestore.transactional
    def acquire(tx):
        snap = lease.get(transaction=tx)
        value = snap.to_dict() or {}
        if value.get('until', now() - timedelta(seconds=1)) > now(): return False
        tx.set(lease, {'token':token,'until':now()+timedelta(seconds=290)})
        return True
    if not await asyncio.to_thread(acquire, db.transaction()): return {'busy':True}
    @firestore.transactional
    def release(tx):
        snap = lease.get(transaction=tx)
        if (snap.to_dict() or {}).get('token') == token: tx.delete(lease)
    try:
        return await asyncio.wait_for(collect_batch(),timeout=240)
    except asyncio.TimeoutError:
        return {'timeout':True, 'message':'Часть материалов сохранена; обработка продолжится в следующем запуске.'}
    finally:
        await asyncio.to_thread(release,db.transaction())

async def collect_batch():
    cfg = await editor_config()
    auto = await setting('autopublish',False)
    started = time.monotonic()
    stats = {'seen':0,'new':0,'prepared':0,'published':0,'errors':0,'filtered':0}
    # Drain old approved pending posts as well as newly collected ones.
    if auto:
        pending = await documents(db.collection('items').where('status','==','pending').limit(30))
        for doc in sorted(pending,key=lambda d:d.to_dict().get('created_at',now())):
            if await publish(doc.id,automatic=True,cfg=cfg):
                stats['published'] += 1
                break
    if not await documents(db.collection('sources').limit(1)):
        for sid,(name,url,category) in DEFAULT_FEEDS.items():
            await asyncio.to_thread(db.collection('sources').document(sid).set,
                                    {'name':name,'url':url,'category':category,'enabled':True})
    sources = await documents(db.collection('sources'))
    entries = []
    for doc in sources:
        source = doc.to_dict()
        if not source.get('enabled',True) or source.get('category','news') not in cfg['categories']: continue
        try:
            data, _ = await media.fetch(source['url'], limit=2_000_000, types=('application/','text/'))
            feed = await asyncio.to_thread(feedparser.parse,data)
            for index, entry in enumerate(feed.entries[:20]): entries.append((index,doc,source,entry))
        except Exception as error:
            log.warning('Source failure: %s (%s)',doc.id,type(error).__name__)
            stats['errors'] += 1
    # Round-robin sources; a busy first feed must not consume the entire batch.
    attempts = 0
    for _, doc, source, entry in sorted(entries,key=lambda row:row[0]):
        if attempts >= cfg['batch_size'] or time.monotonic()-started > 120: break
        stats['seen'] += 1
        link = entry.get('link','')
        if not valid_url(link): continue
        uid = hashlib.sha256((doc.id+'|'+(entry.get('id') or link)).encode()).hexdigest()[:28]
        ref = db.collection('items').document(uid)
        if (await asyncio.to_thread(ref.get)).exists: continue
        title = sanitize(entry.get('title',''),300)
        if not title: continue
        item = {'status':'processing','title':title,'text':'','summary':sanitize(entry.get('summary',''),3000),
                'url':link,'source_id':doc.id,'source_name':source.get('name',doc.id),
                'source_images':source.get('images_allowed',True),'category':source.get('category','news'),
                'created_at':now(),'image_candidates':media.candidates(entry,link),'attempts':1}
        parsed = entry.get('published_parsed') or entry.get('updated_parsed')
        if parsed:
            import calendar
            try: item['source_published_at'] = datetime.fromtimestamp(calendar.timegm(parsed),timezone.utc)
            except (ValueError,OverflowError): pass
        if item.get('source_published_at') and (now()-item['source_published_at']).total_seconds() > cfg['max_age_hours']*3600: continue
        await asyncio.to_thread(ref.create,item)
        stats['new'] += 1
        attempts += 1
        try:
            updated = await asyncio.wait_for(prepare_item(ref,item,cfg),timeout=100)
            stats['filtered' if updated['status']=='filtered' else 'prepared'] += 1
            if auto and updated['status']=='pending' and await publish(uid,automatic=True,cfg=cfg): stats['published'] += 1
        except Exception as error:
            await transition_item(ref,{'status':'awaiting_ai','reason':type(error).__name__,'attempts':1},('processing','awaiting_ai'))
            stats['errors'] += 1
    # Failed items and interrupted processing are retried with a cap; no permanent silent loss.
    if attempts < cfg['batch_size'] and time.monotonic()-started < 120:
        for status in ('awaiting_ai','processing'):
            retry_docs = await documents(db.collection('items').where('status','==',status).limit(10))
            for doc in retry_docs:
                item = doc.to_dict()
                if item.get('attempts',0) >= 3 or attempts >= cfg['batch_size'] or time.monotonic()-started > 120: continue
                attempts += 1
                tries = item.get('attempts',0)+1
                await asyncio.to_thread(doc.reference.update,{'attempts':tries})
                try:
                    updated = await asyncio.wait_for(prepare_item(doc.reference,item,cfg),timeout=100)
                    await asyncio.to_thread(doc.reference.update,{'attempts':tries})
                    stats['filtered' if updated['status']=='filtered' else 'prepared'] += 1
                    if auto and updated['status']=='pending' and await publish(doc.id,automatic=True,cfg=cfg): stats['published'] += 1
                except Exception as error:
                    await transition_item(doc.reference,{'status':'awaiting_ai','reason':type(error).__name__,'attempts':tries},('processing','awaiting_ai'))
                    stats['errors'] += 1
    return stats

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
    edit_key=f'edit_state:{user}'
    state=await setting(edit_key) if text and not text.startswith('/') else None
    if state:
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
                value = await change_config(parts[1],policy.DEFAULTS[parts[1]])
            elif parts[0] == '/set' and len(parts) == 3:
                value = await change_config(parts[1],parts[2])
            else: raise ValueError('Формат: /set ИМЯ значение или /reset ИМЯ')
        except (ValueError,KeyError) as error:
            return await say(chat,str(error))
        return await say(chat,f'Сохранено: {parts[1]} = {value}')
    if text.startswith('/sourceimages '):
        parts = text.split()
        if len(parts) != 3 or parts[2] not in ('on','off'): return await say(chat,'/sourceimages ID on или off')
        ref = db.collection('sources').document(parts[1])
        if not (await asyncio.to_thread(ref.get)).exists: return await say(chat,'Источник не найден')
        await asyncio.to_thread(ref.update,{'images_allowed':parts[2]=='on'})
        return await say(chat,'Правило для будущих материалов источника обновлено. Используйте фото с разрешением правообладателя.')
    if text.startswith('/retry '):
        ref = db.collection('items').document(text.split(maxsplit=1)[1])
        if not await transition_item(ref,{'status':'awaiting_ai','attempts':0},('awaiting_ai','filtered','pending')):
            return await say(chat,'Повтор разрешён для awaiting_ai, filtered и pending.')
        return await say(chat,'Повтор поставлен в очередь следующего сбора.')
    if text.startswith('/addsource '):
        parts=[x.strip() for x in text[len('/addsource '):].split('|')]
        if len(parts)!=3 or not valid_url(parts[1]) or parts[2] not in CATEGORIES:
            return await say(chat,'Формат: /addsource Название | https://example.com/feed.xml | news')
        sid=hashlib.sha256(parts[1].encode()).hexdigest()[:12]
        await asyncio.to_thread(db.collection('sources').document(sid).set, {'name':parts[0][:80],'url':parts[1],'category':parts[2],'enabled':False,'pending_review':True})
        return await say(chat,f'Источник добавлен как {sid}, но отключён до проверки безопасности и формата. Для активации: /enable {sid}')
    for command,enabled in (('/enable ',True),('/disable ',False)):
        if text.startswith(command):
            sid=text[len(command):].strip()
            ref=db.collection('sources').document(sid)
            if not (await asyncio.to_thread(ref.get)).exists: return await say(chat,'Источник не найден')
            await asyncio.to_thread(ref.update, {'enabled':enabled,'pending_review':False})
            return await say(chat,'Источник обновлён')
    if text.startswith('/deletesource '):
        sid=text[len('/deletesource '):].strip()
        await asyncio.to_thread(db.collection('sources').document(sid).delete)
        return await say(chat,'Источник удалён')
    if text=='/sources': return await show_sources(chat)
    if text=='/queue': return await show_queue(chat)
    if text=='/stats': return await show_stats(chat)
    if text=='/help': return await show_help(chat)
    if text=='/auto_on': await put_setting('autopublish',True); return await dashboard(chat)
    if text=='/auto_off': await put_setting('autopublish',False); return await dashboard(chat)
    if text=='/collect':
        await say(chat,'Сбор запущен; это может занять некоторое время.')
        result=await collect()
        return await say(chat,'Результат: '+str(result))
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
        if key not in ('image_mode','review_provider'): return
        cfg = await editor_config()
        choices = policy.CHOICES[key]
        await change_config(key,choices[(choices.index(cfg[key])+1)%len(choices)])
        return await show_settings(chat)
    if data.startswith('source:'):
        ref = db.collection('sources').document(data[7:])
        snap = await asyncio.to_thread(ref.get)
        if snap.exists: await asyncio.to_thread(ref.update,{'enabled':not snap.to_dict().get('enabled',True),'pending_review':False})
        return await show_sources(chat)
    if data.startswith('preview:'): return await preview(chat,data[8:])
    if data=='sources': return await show_sources(chat)
    if data=='queue': return await show_queue(chat)
    if data=='help': return await show_help(chat)
    if data=='stats': return await show_stats(chat)
    if data=='toggle': await put_setting('autopublish',not await setting('autopublish',False)); return await show_settings(chat)
    if data=='collect':
        await say(chat,'Начинаю сбор...')
        return await say(chat,str(await collect()))
    if data.startswith('pub:'): return await say(chat,'Опубликовано.' if await publish(data[4:]) else 'Не опубликовано; проверь статус.')
    if data.startswith('reject:'):
        ref=db.collection('items').document(data[7:])
        if await transition_item(ref, {'status':'rejected'}):
            return await say(chat,'Отклонено')
    if data.startswith('edit:'):
        await put_setting(f'edit_state:{user}',data[5:])
        return await say(chat,'Пришли новый текст одним сообщением. Заголовок и ссылка останутся прежними.')

@app.get('/health')
async def health(): return {'ok':True}

@app.post('/telegram')
async def telegram(request:Request):
    if not secrets.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token',''), WEBHOOK_SECRET):
        raise HTTPException(403)
    update=await request.json()
    started=time.perf_counter()
    try:
        if 'message' in update: await handle_message(update['message'])
        elif 'callback_query' in update: await handle_callback(update['callback_query'])
    except Exception: log.exception('Telegram update failed'); raise HTTPException(500)
    finally:
        log.info('Telegram update %s processed in %.3fs', update.get('update_id'), time.perf_counter()-started)
    return {'ok':True}

@app.post('/collect')
async def collect_endpoint(request:Request):
    if not secrets.compare_digest(request.headers.get('X-Cron-Secret',''),CRON_SECRET): raise HTTPException(403)
    return await collect()
