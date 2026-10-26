"""Safe, actionable API diagnostics. Never expose response bodies, URLs or secrets."""
import asyncio
import re
import httpx
from . import usage


def preflight(provider,cfg):
    if provider in cfg.get('disabled_ai',[]):return 'Провайдер отключён в этом канале. Включите его в меню ИИ.'
    if cfg['cost_mode']=='off':return 'Выбран режим «Без ИИ»: API-запрос не выполнялся. Это штатная работа.'
    if not usage.allowed(provider,cfg[provider+'_model'],cfg):
        if provider=='cloudflare':return 'Workers AI не разрешён: сначала проверьте свой тариф и подтвердите Workers Free в настройках бюджета. Запрос не выполнялся.'
        return 'Бесплатный режим запрещает эту модель. Выберите бесплатную модель OpenRouter, настройте Workers Free или используйте «Без ИИ». Платный режим включается только по вашему решению. Запрос не выполнялся.'
    if provider=='cloudflare' and not re.fullmatch('[a-fA-F0-9]{32}',cfg.get('cloudflare_account_id','')):
        return 'Не задан корректный Cloudflare Account ID (32 символа). API-токен и Account ID — разные настройки. Откройте Настройки → Бюджет → cloudflare_account_id. Запрос не выполнялся.'
    return ''


def explain(error):
    if isinstance(error,usage.BudgetBlocked):return 'Запрос заблокирован режимом расходов или дневным/месячным лимитом ИИ. Проверьте Бюджет и статистику запросов.'
    if isinstance(error,(asyncio.TimeoutError,httpx.TimeoutException)):return 'Истекло время ожидания ответа. Проверьте таймаут модели и повторите позже; это не подтверждает ошибку ключа.'
    status=getattr(getattr(error,'response',None),'status_code',None)
    if status in (401,403):return f'HTTP {status}: ключ отклонён или нет разрешения на модель. Для Cloudflare проверьте разрешение Workers AI и правильный аккаунт.'
    if status==429:return 'HTTP 429: исчерпана квота/баланс или превышена частота запросов. Проверьте кабинет провайдера; переключение на платную модель автоматически не выполняется.'
    if status in (400,404,422):return f'HTTP {status}: проверьте точный ID модели и поддержку формата запроса. Наличие ключа не гарантирует доступ к этой модели.'
    if status and status>=500:return f'HTTP {status}: ошибка сервиса провайдера. Повторите позже или выберите резервную модель.'
    if isinstance(error,httpx.RequestError):return 'Ошибка сети или соединения с API. Повторите позже; проверьте доступ сервера к API.'
    if type(error).__name__ in ('PermissionDenied','Forbidden'):return 'Нет доступа к Secret Manager или Firestore. Проверьте права сервисного аккаунта и запустите диагностику сервера.'
    if isinstance(error,(ValueError,KeyError,TypeError,IndexError)):return 'Ответ модели имеет неподдерживаемый формат или не прошёл проверку. Проверьте модель и настройку JSON; попробуйте резервную модель.'
    return 'Внутренняя ошибка '+type(error).__name__+'. Откройте диагностику. Содержимое ключа и ответа API скрыто.'
