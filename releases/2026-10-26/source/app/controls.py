"""Telegram keyboard labels and route binding; no credentials or network calls."""
NAMES = {'news': 'Новости', 'events': 'События'}
BUTTONS = {
    '📣 Массовая публикация':'/mass',
    '📝 Очередь': '/queue', '🔄 Собрать': '/collect',
    '⚙️ Настройки': '/settings', '🤖 ИИ': '/ai',
    '📡 Источники': '/sources', '🖼 Фото': '/images',
    '📊 Статистика': '/stats', '⚠️ Ошибки': '/failed',
    '🏠 Меню': '/menu',
    '🗂 Неопубликованные':'/unpublished','🔎 Диагностика':'/diagnose','➕ Добавить':'/import',
    '✍️ Создать пост':'/newpost','📢 Каналы':'/channels','🖼 Изображения':'/images',
}
LABELS = {
    'title_template':'Шаблон заголовка {title}','body_template':'Шаблон текста {body}',
    'dedupe_hours':'Проверка повторов, часов','priority_words':'Приоритетные слова','dispatch_batch_size':'Массовых отправок за запуск',
    'daily_limit':'Постов в день','interval_minutes':'Интервал, мин','max_age_hours':'Возраст новости, ч',
    'batch_size':'Публикаций за сбор','processing_batch_size':'Обработать за сбор','body_length':'Длина текста','image_daily_limit':'Генераций в день',
    'event_horizon_days':'Дней вперёд','ai_timeout':'Ожидание ИИ, с','language':'Язык','tone':'Стиль',
    'image_mode':'Режим фото','require_image':'Фото обязательно','image_quality':'Качество картинки',
    'hashtags':'Хештеги темы','source_link':'Ссылка на источник','silent':'Без звука',
    'free_only':'Только бесплатные события','review_provider':'Проверяющий ИИ',
    'cost_mode':'Расходы','text_mode':'Обработка текста','ai_daily_limit':'Запросов ИИ в день',
    'ai_monthly_limit':'Запросов ИИ в месяц','template_style':'Дизайн шаблона','moderation':'Ручная проверка',
    'cloudflare_free_account':'Подтверждаю Workers Free','notifications':'Уведомления',
}
VALUES = {'off':'выкл','on':'вкл','source':'из источника','source_generate':'источник → ИИ','generate':'ИИ',
          'low':'низкое','medium':'среднее','high':'высокое','free':'максимально бесплатно','paid':'платные API разрешены',
          'original':'оригинал без ИИ','auto':'автоматически','translate':'перевод при необходимости',
          'source_template':'фото → бесплатный шаблон','template':'бесплатный шаблон'}


def display(value):
    if type(value) is bool: return 'ВКЛ' if value else 'ВЫКЛ'
    return VALUES.get(str(value),str(value))


def keyboard(route, auto):
    suffix = ' · ' + NAMES[route]
    def button(label): return {'text': label + suffix}
    return {
        'keyboard': [
            [{'text': ('✅ ' if route == 'news' else '') + '📰 Новости'},
             {'text': ('✅ ' if route == 'events' else '') + '📅 События'}],
            [button('📝 Очередь'), button('🔄 Собрать')],
            [button('⚙️ Настройки'), button('🤖 ИИ')],
            [button('📡 Источники'), button('🗂 Неопубликованные')],
            [button('🔎 Диагностика'), button('➕ Добавить')],
            [button('✍️ Создать пост'),button('📣 Массовая публикация')],
            [button('📢 Каналы'),button('🖼 Изображения')],
            [button('⏸ Выключить авто' if auto else '▶️ Включить авто'), button('🏠 Меню')],
        ],
        'resize_keyboard': True, 'is_persistent': True, 'one_time_keyboard': False,
        'input_field_placeholder': NAMES[route] + ' — выберите действие или введите команду',
    }


def command(text):
    for route, name in NAMES.items():
        if text.removeprefix('✅ ') == ('📰 Новости' if route == 'news' else '📅 События'):
            return '/channel ' + route, route
        suffix = ' · ' + name
        if text.endswith(suffix):
            label = text[:-len(suffix)]
            if label in BUTTONS: return BUTTONS[label], route
            if label == '▶️ Включить авто': return '/auto_on', route
            if label == '⏸ Выключить авто': return '/auto_off', route
    return text, None


def scoped(keyboard, route):
    result = []
    for row in keyboard:
        buttons = []
        for original in row:
            button = dict(original)
            data = button.get('callback_data', '')
            if data and not data.startswith(('ctx:', 'route:', 'pub:', 'preview:', 'reject:', 'edit:', 'status:', 'retry:', 'reconcile:', 'reconcileok:', 'photo:', 'select:')):
                button['callback_data'] = 'ctx:' + route + '|' + data
            if len(button.get('callback_data', '').encode()) > 64:
                raise ValueError('Callback data exceeds Telegram limit')
            buttons.append(button)
        result.append(buttons)
    return result
