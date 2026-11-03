#!/usr/bin/env python3
"""Apply additive, non-Git, source-only fixes to Finland News Bot v2.
Safe: checks exact source anchors and Python syntax before replacing files.
Does not connect to Google, change Firestore, or deploy automatically.
"""
from pathlib import Path
import ast
import shutil
from datetime import datetime
import sys

root = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv)>1 else Path.home()/'projects'/'bot'
paths = {name: root/'app'/name for name in ('main.py','policy.py','media.py')}
for name, path in paths.items():
    if not path.is_file():
        raise SystemExit(f'Not found: {path}; expected ~/projects/bot')
code = {k: p.read_text(encoding='utf-8') for k,p in paths.items()}
if 'def offline_prepare(' in code['main.py']:
    raise SystemExit('BotFix already appears to be installed; no changes made.')

def replace(name, old, new):
    count = code[name].count(old)
    if count != 1:
        raise SystemExit(f'Unexpected source in {name}: expected one occurrence, found {count}. Nothing changed.')
    code[name] = code[name].replace(old,new,1)

# 1. Explicit no-AI and failover modes, selectable through Telegram settings.
replace('policy.py',
    "'image_quality': 'low',",
    "'image_quality': 'low', 'ai_mode': 'auto', 'no_ai_autopublish': False,")
replace('policy.py',
    "CHOICES = {'image_mode': ('off', 'source', 'source_generate', 'generate'),",
    "CHOICES = {'image_mode': ('off', 'source', 'source_generate', 'generate', 'source_template', 'template'),\n           'ai_mode': ('auto', 'off', 'fallback'),")
replace('main.py',
    "'ai': ('🤖 ИИ и проверка', ['ai_order','ai_timeout','review_provider']+[p+'_model' for p in policy.PROVIDERS]),",
    "'ai': ('🤖 ИИ и проверка', ['ai_mode','no_ai_autopublish','ai_order','ai_timeout','review_provider']+[p+'_model' for p in policy.PROVIDERS]),")
replace('main.py',
    "    if section == 'ai':\n        lines += ['\\nКлючи подключены (доступ к модели проверяется при запросе):']",
    "    if section == 'ai':\n        lines += ['\\nai_mode=off: полностью без ИИ; язык текста сохраняется как у источника. ' \
                  'fallback: попытка ИИ, затем исходный текст. Для нулевых затрат: /set ai_mode off']\n        lines += ['\\nКлючи подключены (доступ к модели проверяется при запросе):']")

# 2. Deterministic no-AI article preparation; retain moderated news and verified event gates.
insert = '''def offline_prepare(item, cfg):
    """Free editorial fallback: no API calls, no fabricated translation or event metadata.

    News are marked sensitive for human approval; future official events may go
    through the existing date/location/auto-publication rules.
    """
    original_title = sanitize(item.get('title', ''), 300)
    original_body = sanitize(item.get('summary') or item.get('text') or original_title,
                             max(150, cfg['body_length']))
    route = route_of(item)
    event_ok = route == 'events' and not policy.event_reason(item, now())
    unsafe_keywords = ('kela', 'migri', 'poliisi', 'police', 'terveys', 'health',
                       'medical', 'war', 'sota', 'asylum', 'turvapaikka',
                       'terror', 'rikos', 'crime', 'lääke', 'medicine', 'kuolema')
    needs_review = any(word in (original_title + ' ' + original_body).casefold() for word in unsafe_keywords)
    news_autopublish = (route == 'news' and cfg.get('no_ai_autopublish') is True
                        and not needs_review)
    data = {
        'title': original_title,
        'body': original_body or original_title,
        'category': item.get('category', 'news'),
        'topic': 'Джерело без ШІ',
        'sensitive': not (event_ok or news_autopublish),
        'has_missing_event_details': route == 'events' and not event_ok,
        'review_passed': False,
        'review_provider': 'off',
        'review_reason': 'Збережено оригінальну мову джерела; ШІ не використовувався',
        'ai_provider': 'off',
        'ai_model': '',
    }
    if cfg['language'] == 'українська + suomi':
        # We cannot claim a Ukrainian translation when AI is disabled.
        # Show the original content once instead of duplicating as two languages.
        data.update(title_fi='', body_fi='')
    return data

'''
replace('main.py',
    'async def prepare_item(ref, item, cfg):',
    insert + 'async def prepare_item(ref, item, cfg):')
replace('main.py',
    "    ai = await asyncio.wait_for(ai_prepare(item['title'], context, item['url'], item['category'], cfg),80)\n    if ai is None: raise ValueError('All configured AI writers unavailable')",
    "    mode = cfg.get('ai_mode', 'auto')\n    ai = (offline_prepare(item, cfg) if mode == 'off' else\n          await asyncio.wait_for(ai_prepare(item['title'], context, item['url'], item['category'], cfg),80))\n    if ai is None and mode == 'fallback': ai = offline_prepare(item, cfg)\n    if ai is None: raise ValueError('All configured AI writers unavailable')")

# 3. Free local image template as an alternative to AI; source images remain first.
media_add = '''def template_image(item):
    """Create a royalty-free abstract editorial card entirely locally with Pillow."""
    from PIL import ImageDraw, ImageFont
    from textwrap import wrap
    import io
    canvas = Image.new('RGB', (1200, 675), '#132b48')
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, 1200, 20), fill='#68b9d9')
    draw.polygon([(0, 675), (0, 380), (460, 675)], fill='#1c476b')
    draw.polygon([(1200, 0), (790, 0), (1200, 385)], fill='#265a71')
    draw.ellipse((860, 240, 1210, 590), fill='#306a77')
    draw.ellipse((1010, 405, 1195, 590), fill='#f9d34f')
    draw.rounded_rectangle((70, 110, 750, 560), radius=25, fill='#0d233d')
    try:
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 45)
        tiny = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 25)
    except OSError:
        font = ImageFont.load_default()
        tiny = ImageFont.load_default()
    draw.text((112, 146), 'FINLAND  |  SUOMI', font=tiny, fill='#82cfed')
    title = str(item.get('title') or 'Finland News').strip()
    lines = []
    for paragraph in wrap(title, 23, break_long_words=True, break_on_hyphens=False):
        lines.append(paragraph)
        if len(lines) == 5: break
    for index, line in enumerate(lines):
        try:
            draw.text((112, 220 + index * 60), line, font=font, fill='#ffffff')
        except UnicodeEncodeError:
            # No usable Unicode font in slim images: keep the abstract picture.
            break
    draw.text((900, 615), 'EDITORIAL ART', font=tiny, fill='#ffffff')
    output = io.BytesIO()
    canvas.save(output, 'JPEG', quality=78, optimize=True)
    payload = output.getvalue()
    if len(payload) > 450_000: raise ValueError('Template exceeds Firestore media limit')
    return {'image_bytes': payload, 'image_kind': 'template', 'image_model': 'Pillow (free)', 'image_provider': 'local'}

'''
replace('media.py','def image_prompt(item):',media_add+'def image_prompt(item):')
replace('main.py',
    "    mode = cfg['image_mode']\n    if mode == 'off': return {}\n    if mode in ('source', 'source_generate') and item.get('source_images', True):",
    "    mode = cfg['image_mode']\n    if mode == 'off': return {}\n    if mode == 'template': return await asyncio.to_thread(media.template_image, item)\n    if mode in ('source', 'source_generate', 'source_template') and item.get('source_images', True):")
replace('main.py',
    "    errors = []\n    if mode in ('generate', 'source_generate'):",
    "    if mode == 'source_template':\n        return await asyncio.to_thread(media.template_image, item)\n    errors = []\n    if mode in ('generate', 'source_generate'):")

# 4. Queue shows publishable events first; invalid/undated ones appear in /incomplete.
replace('main.py',
    "key=lambda d:d.to_dict().get('created_at',now()))[:limit]",
    "key=lambda d:d.to_dict().get('created_at',now()),reverse=True)[:limit]")
replace('main.py',
    "async def show_queue(chat, page=0):\n    docs = await item_docs('pending',limit=1000)\n    if not docs: return await say(chat,'Очередь пуста. /failed — ошибки; /collect — собрать материалы.')",
    "async def show_queue(chat, page=0):\n    docs = await item_docs('pending',limit=1000)\n    if ROUTE.get() == 'events':\n        docs = [d for d in docs if not policy.event_reason(d.to_dict(), now())]\n    if not docs: return await say(chat,'Нет готовых материалов. /incomplete — события без даты/места; /failed — ошибки; /collect — сбор.')")
replace('main.py',
    "async def show_stats(chat):",
    """async def show_incomplete(chat):
    if ROUTE.get() != 'events':
        return await say(chat, 'Команда /incomplete доступна для канала событий: /channel events')
    docs = await item_docs('pending', limit=1000)
    missing = [(d, policy.event_reason(d.to_dict(), now())) for d in docs]
    missing = [(d, reason) for d, reason in missing if reason]
    lines = [f'⚠️ События, требующие проверки: {len(missing)} (из последних 1000 ожидающих).']
    for d, reason in missing[:12]:
        lines.append(f'{d.id}: {reason} — {d.to_dict().get("title", "")[:75]}')
    lines.append('Проверяйте официальную дату и место до команды /event ID | YYYY-MM-DDTHH:MM | Helsinki | Место | цена')
    return await say(chat, '\\n'.join(lines))

async def show_stats(chat):""")
replace('main.py',
    "    if text == '/failed': return await show_failed(chat)",
    "    if text == '/failed': return await show_failed(chat)\n    if text == '/incomplete': return await show_incomplete(chat)")
replace('main.py',
    "    if data.startswith('pub:'): return await say(chat,'Опубликовано.' if await publish(data[4:]) else 'Не опубликовано; проверь статус.')",
    """    if data.startswith('pub:'):
        docid = data[4:]
        snap = await asyncio.to_thread(db.collection('items').document(docid).get)
        if snap.exists and route_of(snap.to_dict()) == 'events':
            reason = policy.event_reason(snap.to_dict(), now())
            if reason:
                return await say(chat, 'Не опубликовано: ' + reason +
                       '. /incomplete — список; /event ID | YYYY-MM-DDTHH:MM | Helsinki | Место | цена — после проверки фактов.')
        return await say(chat, 'Опубликовано.' if await publish(docid) else
                         'Не опубликовано. Посмотри /failed и журналы Cloud Run; пост мог уже уйти в канал.')""")
replace('main.py',
    "        '/sources /queue /stats /failed /collect\\n/auto_on /auto_off — выбранный канал\\n/retry ID — повторить обработку\\n'",
    "        '/sources /queue /incomplete /stats /failed /collect\\n/auto_on /auto_off — выбранный канал\\n/retry ID — повторить обработку\\n'")
# Avoid misleading label derived from git commit when deployment includes uncommitted patches.
upgrade = root/'scripts'/'upgrade-all.sh'
upsrc = upgrade.read_text(encoding='utf-8') if upgrade.is_file() else None
if upsrc and 'APP_VERSION=$(git rev-parse --short HEAD)' in upsrc:
    upsrc = upsrc.replace('APP_VERSION=$(git rev-parse --short HEAD)',
                          'APP_VERSION=${APP_VERSION_OVERRIDE:-$(git rev-parse --short HEAD)}',1)

# Validate changed Python source BEFORE touching user's files.
for k,src in code.items():
    ast.parse(src, filename=str(paths[k]))
backup_dir=root/('backup-botfix-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
backup_dir.mkdir(parents=False)
for k,path in paths.items():
    shutil.copy2(path,backup_dir/k)
if upsrc is not None:
    shutil.copy2(upgrade,backup_dir/'upgrade-all.sh')
for k,path in paths.items():
    path.write_text(code[k],encoding='utf-8')
if upsrc is not None:
    upgrade.write_text(upsrc,encoding='utf-8')
print('PATCH APPLIED; backup:',backup_dir)
print('No Git commands were used. No Firestore records changed. No deployment done.')
print('Use /channel events then /incomplete to view undated events.')
print('For free no-AI: /set ai_mode off, /set image_mode source_template, /set require_image on')
print('Optional news auto-publishing originals: /set no_ai_autopublish on (after reviewing samples)')
