"""Telegram HTML built only from escaped text; limits count visible UTF-16 units."""
import html
from . import post_templates
from urllib.parse import urlparse,quote
from zoneinfo import ZoneInfo

LABELS = {'news': 'Новини · Uutiset', 'ukraine': 'Українська громада', 'sport': 'Спорт · Urheilu',
          'children': 'Дітям · Lapsille', 'events': 'Події · Tapahtumat',
          'transport': 'Транспорт · Liikenne', 'other': 'Фінляндія · Suomi'}
TAGS = {'news':'#НовиниФінляндії','ukraine':'#УкраїнціУФінляндії','sport':'#Спорт',
        'children':'#Дітям','events':'#Події','transport':'#Транспорт','other':'#Фінляндія'}

def units(text): return len(text.encode('utf-16-le')) // 2

def fit(text, limit):
    return str(text).encode('utf-16-le')[:max(0,limit)*2].decode('utf-16-le', errors='ignore')

def clip(text, limit):
    text = str(text)
    return text if units(text) <= limit else fit(text, limit-1).rstrip() + '…'

def render(item, cfg, channel_tag, photo=False):
    # Pairs of visible text and optional trusted formatting. Never truncate serialized HTML.
    esc = html.escape
    limit = 1024 if photo else 4096
    title_limit = 90 if photo else 180
    head_text = clip(post_templates.render(cfg.get('title_template','{title}'),title=item.get('title','Фінляндія'),category=LABELS.get(item.get('category'),'Фінляндія')), title_limit)
    topic = clip(item.get('topic') or LABELS.get(item.get('category'), 'Фінляндія'), 45)
    head = f'<b>{esc(head_text)}</b>\n<i>{esc(topic)}</i>\n\n'
    head_size = units(head_text + '\n' + topic + '\n\n')
    footer_parts = []
    event = item.get('event', {})
    details = []
    if event.get('start'):
        local = event['start'].astimezone(ZoneInfo(cfg['timezone']))
        details.append(('📅 Коли / Milloin: ', local.strftime('%d.%m.%Y' if event.get('date_only') else '%d.%m.%Y %H:%M')))
    place = ', '.join(dict.fromkeys(str(event[k]) for k in ('venue','address','city') if event.get(k)))
    if place: details.append(('📍 Де / Missä: ', clip(place, 130)))
    if event.get('online'): details.append(('🌐 ', 'Онлайн / Verkossa'))
    if event.get('is_free') is True: details.append(('🎟 ', 'Безкоштовно / Maksuton'))
    elif event.get('price'): details.append(('🎟 ', clip(event['price'], 60)))
    if event.get('time_note'): details.append(('🕒 ',clip(event['time_note'],80)))
    if event.get('sold_out'): details.append(('⚠️ ', 'Розпродано / Loppuunmyyty'))
    if event.get('age_limit'):details.append(('👤 ',clip(event['age_limit'],35)))
    if event.get('contact'):details.append(('📞 ',clip(event['contact'],90)))
    for label, value in details:
        footer_parts.append((label + value, '<b>'+esc(label)+'</b>'+esc(value)))
    if photo and item.get('image_kind') == 'generated':
        footer_parts.append(('🎨 Ілюстрація ШІ / AI-kuvitus', '🎨 Ілюстрація ШІ / AI-kuvitus'))
    elif photo and item.get('image_kind') == 'source':
        credit = '📷 ' + clip(item.get('image_credit') or item.get('source_name','Джерело'),70)
        footer_parts.append((credit, esc(credit)))
    url = item.get('url','')
    if cfg['source_link'] and urlparse(url).scheme == 'https':
        label = '🔗 Джерело / Lähde: '+clip(item.get('source_name') or urlparse(url).hostname or 'Деталі',50)
        footer_parts.append((label, '<a href="'+esc(url,quote=True)+'">'+esc(label)+'</a>'))
    ticket=event.get('ticket_url','')
    if ticket and ticket!=url and urlparse(ticket).scheme=='https':
        footer_parts.append(('🎟 Квитки / Liput','<a href="'+esc(ticket,quote=True)+'">🎟 Квитки / Liput</a>'))
    if place:
        maps='https://www.google.com/maps/search/?api=1&query='+quote(place)
        transit='https://www.google.com/maps/dir/?api=1&travelmode=transit&destination='+quote(place)
        footer_parts.append(('🗺 Карта · Як дістатися','<a href="'+esc(maps,quote=True)+'">🗺 Карта</a> · <a href="'+esc(transit,quote=True)+'">Як дістатися</a>'))
    if cfg['signature']:
        signature = clip(cfg['signature'],100)
        footer_parts.append((signature,esc(signature)))
    tags = (TAGS.get(item.get('category'),'#Фінляндія')+' ' if cfg['hashtags'] else '')+channel_tag
    footer_parts.append((tags,esc(tags)))
    # Long contact/price/signature fields must not make a caption invalid. Keep attribution,
    # channel identity, date and venue; optional details remain accessible in the source.
    while head_size+units('\n\n'+'\n'.join(p[0] for p in footer_parts))+80>limit:
        optional=[i for i,p in enumerate(footer_parts[:-1]) if not p[0].startswith(('🔗','📷','🎨','📅','📍'))]
        if not optional:break
        footer_parts.pop(optional[-1])
    footer_text = '\n\n' + '\n'.join(p[0] for p in footer_parts)
    footer_html = '\n\n' + '\n'.join(p[1] for p in footer_parts)
    available = max(0, limit - head_size - units(footer_text))
    bilingual = cfg['language'] == 'українська + suomi' and bool(item.get('title_fi') or item.get('text_fi')) and available>15
    if bilingual:
        fi_title = clip(item.get('title_fi',''), min(title_limit,max(0,available-15)))
        # Reserve the second-language title and separator before allocating both bodies.
        room = max(0, available - units('\n\n🇫🇮 '+fi_title+'\n'))
        ua = clip(item.get('text',''), min(cfg['body_length']//2, room//2)) if room >= 4 else ''
        fi = clip(item.get('text_fi',''), min(cfg['body_length']//2, room-room//2)) if room >= 4 else ''
        middle = esc(ua)+'\n\n<b>🇫🇮 '+esc(fi_title)+'</b>\n'+esc(fi)
    else:
        middle = esc(clip(post_templates.render(cfg.get('body_template','{body}'),body=item.get('text','')), min(cfg['body_length'],available))) if available else ''
    return head+middle+footer_html
