"""Validated editor settings and deterministic publication rules."""
from datetime import datetime
from copy import deepcopy
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CATEGORY_NAMES = {'news': 'Новости', 'ukraine': 'Украинцы', 'sport': 'Спорт',
                  'children': 'Дети', 'events': 'События', 'transport': 'Транспорт', 'other': 'Другое'}
DEFAULTS = {
    'categories': list(CATEGORY_NAMES), 'daily_limit': 12, 'interval_minutes': 60,
    'window_start': '08:00', 'window_end': '22:00', 'timezone': 'Europe/Helsinki',
    'weekdays': [1, 2, 3, 4, 5, 6, 7], 'max_age_hours': 48,
    'include_words': '', 'exclude_words': '', 'batch_size': 3, 'processing_batch_size':10, 'dedupe_hours':168, 'dispatch_batch_size':3,'priority_words':'', 'body_length': 650,
    'language': 'українська', 'tone': 'нейтральний', 'signature': '', 'title_template':'{title}', 'body_template':'{body}',
    'hashtags': True, 'source_link': True, 'silent': False,
    'image_mode': 'source_template', 'require_image': True, 'image_daily_limit': 3,
    'image_model': 'gemini-3.1-flash-image', 'ai_order': ['gemini', 'groq', 'openai'],
    'image_order': ['openai', 'gemini'], 'openai_image_model': 'gpt-image-2.5-flare',
    'image_quality': 'low',
    'review_provider': 'off', 'disabled_ai': [], 'gemini_model': 'gemini-3.5-flash-lite',
    'groq_model': 'openai/gpt-oss-20b', 'openai_model': 'gpt-4.1-mini',
    'deepseek_model': 'deepseek-flash', 'mistral_model': 'mistral-small-latest',
    'openrouter_model': 'openrouter/free', 'ai_timeout': 12,
    'cloudflare_model':'@cf/qwen/qwen3-30b-a3b-fp8','huggingface_model':'openai/gpt-oss-120b',
    'cloudflare_account_id':'','cloudflare_free_account':False,
    'cloudflare_image_model':'@cf/black-forest-labs/flux-1-schnell',
    'cost_mode':'free','text_mode':'auto','ai_daily_limit':30,'ai_monthly_limit':300,
    'template_style':'auto','moderation':False,'region_words':'','notifications':True,
    'event_horizon_days': 30, 'event_cities': 'Helsinki,Espoo,Vantaa',
    'free_only': False, 'event_languages': '', 'channel_tag': '',
}
PROVIDERS = ('gemini', 'groq', 'openai', 'deepseek', 'mistral', 'openrouter','cloudflare','huggingface')
DEFAULTS['ai_order'] = list(PROVIDERS)
DEFAULTS['translation_order'] = []
for provider in PROVIDERS:DEFAULTS[provider+'_backup_model']=''
EVENT_DEFAULTS = {'categories': ['events'], 'language': 'українська + suomi',
                  'daily_limit': 20, 'interval_minutes': 30, 'body_length': 550}
RANGES = {'daily_limit': (0, 100), 'interval_minutes': (0, 1440), 'max_age_hours': (1, 720),
          'batch_size': (1, 10), 'processing_batch_size':(1,50), 'dedupe_hours':(24,2160),'dispatch_batch_size':(1,10), 'body_length': (150, 2500), 'image_daily_limit': (0, 30),
          'event_horizon_days': (1, 180), 'ai_timeout': (5, 25),
          'ai_daily_limit':(0,1000),'ai_monthly_limit':(0,10000)}
CHOICES = {'image_mode': ('source_template','template','source_generate','generate','off','source'),
           'cost_mode':('off','free','paid'),'text_mode':('auto','original','translate'),
           'template_style':('auto','news','sport','events'),
           'image_quality': ('low', 'medium', 'high'),
           'review_provider': ('off',) + PROVIDERS,
           'language': ('українська', 'suomi', 'українська + suomi', 'русский', 'English'),
           'tone': ('нейтральний', 'коротко', 'дружній')}

def validate(key, value):
    if key not in DEFAULTS:
        raise ValueError('Неизвестная настройка. /settings — список.')
    if key in RANGES:
        try: result = int(value)
        except (ValueError, TypeError): raise ValueError('Нужно целое число.')
        low, high = RANGES[key]
        if not low <= result <= high: raise ValueError(f'Допустимо от {low} до {high}.')
        return result
    if type(DEFAULTS[key]) is bool:
        if value in (True, 'on', 'true', '1'): return True
        if value in (False, 'off', 'false', '0'): return False
        raise ValueError('Используйте on или off.')
    if key in ('categories', 'ai_order', 'image_order', 'weekdays', 'disabled_ai','translation_order'):
        result = value if isinstance(value, list) else [v.strip() for v in str(value).split(',') if v.strip()]
        if key == 'weekdays':
            try: result = [int(v) for v in result]
            except ValueError: raise ValueError('Дни: 1,2,3,4,5,6,7 (понедельник–воскресенье).')
        allowed = set(CATEGORY_NAMES) if key == 'categories' else set(range(1, 8)) if key == 'weekdays' else {'openai','gemini','cloudflare'} if key == 'image_order' else set(PROVIDERS)
        if not set(result) <= allowed or (key in ('ai_order','image_order') and not result):
            raise ValueError('Допустимо: ' + ', '.join(map(str, sorted(allowed))))
        return list(dict.fromkeys(result))
    value = str(value).strip()
    if value == '-': value = ''
    if key in ('title_template','body_template'):
        from . import post_templates
        post_templates.validate(key,value)
    if key in CHOICES and value not in CHOICES[key]:
        raise ValueError('Варианты: ' + ', '.join(CHOICES[key]))
    if key in ('window_start', 'window_end'):
        try: datetime.strptime(value, '%H:%M')
        except ValueError: raise ValueError('Время в формате HH:MM.')
        if len(value) != 5: raise ValueError('Время в формате HH:MM.')
    if key == 'timezone':
        try: ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError): raise ValueError('Нужна зона IANA, например Europe/Helsinki.')
    if key.endswith('_backup_model') and not value:return ''
    if key.endswith('_model'):
        import re
        if not re.fullmatch(r'[@A-Za-z0-9._/:-]{1,120}', value): raise ValueError('Некорректное имя модели.')
    if key=='cloudflare_account_id' and value:
        import re
        if not re.fullmatch('[a-fA-F0-9]{32}',value): raise ValueError('Account ID Cloudflare: 32 шестнадцатеричных символа.')
    if key == 'channel_tag' and value:
        import re
        if not re.fullmatch(r'#?\w{2,60}', value): raise ValueError('Хештег: буквы, цифры, подчёркивание; 2–60 знаков.')
    if len(value) > (150 if key == 'signature' else 500): raise ValueError('Значение слишком длинное.')
    return value

def config(stored=None, route='news'):
    result = deepcopy(DEFAULTS)
    if route == 'events': result.update(deepcopy(EVENT_DEFAULTS))
    for key, value in (stored or {}).items():
        try: result[key] = validate(key, value)
        except ValueError: pass
    return result

def topic_allowed(item, cfg):
    if item.get('category', 'other') not in cfg['categories']: return False
    text = (item.get('title', '') + ' ' + item.get('text', '')).casefold()
    include = [v.strip().casefold() for v in cfg['include_words'].split(',') if v.strip()]
    exclude = [v.strip().casefold() for v in cfg['exclude_words'].split(',') if v.strip()]
    regions=[v.strip().casefold() for v in cfg.get('region_words','').split(',') if v.strip()]
    if regions and not any(v in text for v in regions): return False
    if item.get('route') == 'events' or item.get('category')=='events':
        event = item.get('event', {})
        cities = [v.strip().casefold() for v in cfg['event_cities'].split(',') if v.strip()]
        if cities and event.get('city') and event['city'].casefold() not in cities: return False
        if cfg['free_only'] and event.get('is_free') is not True: return False
        languages = {v.strip().casefold() for v in cfg['event_languages'].split(',') if v.strip()}
        if languages and not languages.intersection(event.get('languages', [])): return False
    return (not include or any(v in text for v in include)) and not any(v in text for v in exclude)

def event_expired(item, current):
    event=item.get('event',{})
    start=event.get('start')
    if not isinstance(start,datetime): return False
    if event.get('date_only'): return start.astimezone(ZoneInfo('Europe/Helsinki')).date()<current.astimezone(ZoneInfo('Europe/Helsinki')).date()
    return start<=current

def event_reason(item, current):
    if item.get('route') != 'events' and item.get('category')!='events': return ''
    event = item.get('event', {})
    if event.get('cancelled'): return 'Событие отменено'
    start = event.get('start')
    if not isinstance(start, datetime): return 'Нет подтверждённой даты события'
    if event_expired(item,current): return 'Событие уже началось'
    if not event.get('venue') and not event.get('online'): return 'Нет места события'
    return ''

def automatic_reason(item, cfg, current, ledger):
    if cfg.get('moderation'): return 'Включено ручное подтверждение всех материалов'
    if item.get('translation_missing'): return 'Нет перевода на выбранный язык'
    if event_reason(item, current): return event_reason(item, current)
    if not topic_allowed(item, cfg): return 'Тема или ключевые слова отключены'
    if item.get('sensitive', True) or item.get('missing_details', False): return 'Нужна модерация'
    if cfg['cost_mode'] != 'off' and cfg['review_provider'] != 'off' and (not item.get('review_passed', False) or item.get('review_provider') != cfg['review_provider']):
        return 'Нет одобрения проверяющего ИИ'
    if cfg['cost_mode'] != 'off' and cfg['review_provider'] in cfg['disabled_ai']: return 'Проверяющий ИИ отключён в этом канале'
    # The publisher creates a local image before claiming the send; absence alone never drops a post.
    created = item.get('source_published_at') or item.get('created_at')
    if item.get('route') != 'events' and created and (current - created).total_seconds() > cfg['max_age_hours'] * 3600: return 'Новость устарела'
    local = current.astimezone(ZoneInfo(cfg['timezone']))
    if local.isoweekday() not in cfg['weekdays']: return 'Сегодня публикации отключены'
    clock, start, end = local.strftime('%H:%M'), cfg['window_start'], cfg['window_end']
    in_window = start == end or (start <= clock < end if start < end else clock >= start or clock < end)
    if not in_window: return 'Вне времени публикации'
    day = local.date().isoformat()
    if (ledger.get('count', 0) if ledger.get('day') == day else 0) >= cfg['daily_limit']: return 'Дневной лимит'
    last = ledger.get('last_at')
    if last and (current - last).total_seconds() < cfg['interval_minutes'] * 60: return 'Интервал между публикациями'
    return ''


def priority(item,cfg):
    text=(item.get('title','')+' '+item.get('summary','')+' '+item.get('text','')).casefold()
    words=[w.strip().casefold() for w in cfg.get('priority_words','').split(',') if w.strip()]
    return sum(1 for word in words if word in text)
