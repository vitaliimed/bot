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
    'include_words': '', 'exclude_words': '', 'batch_size': 3, 'body_length': 650,
    'language': 'українська', 'tone': 'нейтральний', 'signature': '',
    'hashtags': True, 'source_link': True, 'silent': False,
    'image_mode': 'source_generate', 'require_image': False, 'image_daily_limit': 3,
    'image_model': 'gemini-3.1-flash-image', 'ai_order': ['gemini', 'groq', 'openai'],
    'image_order': ['openai', 'gemini'], 'openai_image_model': 'gpt-image-2.5-flare',
    'image_quality': 'low',
    'review_provider': 'off', 'gemini_model': 'gemini-3.5-flash-lite',
    'groq_model': 'openai/gpt-oss-20b', 'openai_model': 'gpt-4.1-mini',
    'deepseek_model': 'deepseek-flash', 'mistral_model': 'mistral-small-latest',
    'openrouter_model': 'openai/gpt-4.1-mini', 'ai_timeout': 12,
    'event_horizon_days': 30, 'event_cities': 'Helsinki,Espoo,Vantaa',
    'free_only': False, 'event_languages': '', 'channel_tag': '',
}
PROVIDERS = ('gemini', 'groq', 'openai', 'deepseek', 'mistral', 'openrouter')
DEFAULTS['ai_order'] = list(PROVIDERS)
EVENT_DEFAULTS = {'categories': ['events'], 'language': 'українська + suomi',
                  'daily_limit': 20, 'interval_minutes': 30, 'body_length': 550}
RANGES = {'daily_limit': (0, 100), 'interval_minutes': (0, 1440), 'max_age_hours': (1, 720),
          'batch_size': (1, 10), 'body_length': (150, 2500), 'image_daily_limit': (0, 30),
          'event_horizon_days': (1, 180), 'ai_timeout': (5, 25)}
CHOICES = {'image_mode': ('off', 'source', 'source_generate', 'generate'),
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
    if key in ('categories', 'ai_order', 'image_order', 'weekdays'):
        result = value if isinstance(value, list) else [v.strip() for v in str(value).split(',') if v.strip()]
        if key == 'weekdays':
            try: result = [int(v) for v in result]
            except ValueError: raise ValueError('Дни: 1,2,3,4,5,6,7 (понедельник–воскресенье).')
        allowed = set(CATEGORY_NAMES) if key == 'categories' else set(range(1, 8)) if key == 'weekdays' else {'openai','gemini'} if key == 'image_order' else set(PROVIDERS)
        if not set(result) <= allowed or (key in ('ai_order','image_order') and not result):
            raise ValueError('Допустимо: ' + ', '.join(map(str, sorted(allowed))))
        return list(dict.fromkeys(result))
    value = str(value).strip()
    if value == '-': value = ''
    if key in CHOICES and value not in CHOICES[key]:
        raise ValueError('Варианты: ' + ', '.join(CHOICES[key]))
    if key in ('window_start', 'window_end'):
        try: datetime.strptime(value, '%H:%M')
        except ValueError: raise ValueError('Время в формате HH:MM.')
        if len(value) != 5: raise ValueError('Время в формате HH:MM.')
    if key == 'timezone':
        try: ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError): raise ValueError('Нужна зона IANA, например Europe/Helsinki.')
    if key.endswith('_model'):
        import re
        if not re.fullmatch(r'[A-Za-z0-9._/-]{1,120}', value): raise ValueError('Некорректное имя модели.')
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
    if item.get('route') == 'events':
        event = item.get('event', {})
        cities = [v.strip().casefold() for v in cfg['event_cities'].split(',') if v.strip()]
        if cities and event.get('city') and event['city'].casefold() not in cities: return False
        if cfg['free_only'] and event.get('is_free') is not True: return False
        languages = {v.strip().casefold() for v in cfg['event_languages'].split(',') if v.strip()}
        if languages and not languages.intersection(event.get('languages', [])): return False
    return (not include or any(v in text for v in include)) and not any(v in text for v in exclude)

def event_reason(item, current):
    if item.get('route') != 'events': return ''
    event = item.get('event', {})
    if event.get('cancelled'): return 'Событие отменено'
    start = event.get('start')
    if not isinstance(start, datetime): return 'Нет подтверждённой даты события'
    if start <= current: return 'Событие уже началось'
    if not event.get('venue') and not event.get('online'): return 'Нет места события'
    return ''

def automatic_reason(item, cfg, current, ledger):
    if event_reason(item, current): return event_reason(item, current)
    if not topic_allowed(item, cfg): return 'Тема или ключевые слова отключены'
    if item.get('sensitive', True) or item.get('missing_details', False): return 'Нужна модерация'
    if cfg['review_provider'] != 'off' and (not item.get('review_passed', False) or item.get('review_provider') != cfg['review_provider']):
        return 'Нет одобрения проверяющего ИИ'
    if cfg['require_image'] and not item.get('image_bytes') and not item.get('image_file_id'): return 'Нет изображения'
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
