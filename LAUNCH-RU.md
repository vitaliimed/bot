# Finland News Bot — запуск в Google Cloud Shell

## Подготовка
1. Создать **отдельный** Google Cloud проект с включённым billing. Не использовать клиентский SVM Rakennus или AutoHelp.
2. Через @BotFather создать Telegram-бота и получить токен. **Никому не отправлять токен, в том числе в чат.**
3. Создать публичный канал, назначить бота администратором с правом публикации. Записать `@username_канала`.
4. Узнать собственный **числовой** Telegram user ID, не username.
5. Создать Gemini API key через Google AI Studio и проверить доступность бесплатной модели/квоты.
6. В Cloud Shell загрузить ZIP, распаковать, перейти в каталог с Dockerfile.

## Команды в Cloud Shell
```bash
unzip finland-news-bot-cloudrun.zip -d finland-news-bot
cd finland-news-bot
export PROJECT_ID='YOUR_SEPARATE_PROJECT_ID'
export ADMIN_ID='YOUR_NUMERIC_TELEGRAM_USER_ID'
export CHANNEL_ID='@YOUR_PUBLIC_CHANNEL'
export REGION='europe-north1'
bash scripts/preflight.sh
read -rsp 'Telegram bot token: ' BOT_TOKEN; echo; export BOT_TOKEN
read -rsp 'Gemini API key: ' GEMINI_API_KEY; echo; export GEMINI_API_KEY
export WEBHOOK_SECRET="$(openssl rand -hex 24)"
export CRON_SECRET="$(openssl rand -hex 24)"
bash scripts/deploy.sh
unset BOT_TOKEN GEMINI_API_KEY WEBHOOK_SECRET CRON_SECRET
```
**Важно:** при первом запуске Cloud Build может потребовать дополнительных IAM разрешений; Cloud Shell выведет конкретную ошибку. Firestore database `(default)` создаётся однократно. Не запускайте deploy повторно без проверки последствий для webhook и секретов.

## После развёртывания
1. Написать боту `/start` с аккаунта администратора.
2. Включить и проверить источники: `/sources`; выполнить `/collect`.
3. Открыть `/queue`, проверить перевод и отправить 1 тестовую публикацию.
4. Только после успешной проверки включить `/auto_on`.
5. Проверить Cloud Scheduler job `finland-news-collect`, логи Cloud Run и наличие сообщений в канале.

## Управление
`/start`, `/sources`, `/addsource Название | https://example.com/feed | news`, `/enable ID`, `/disable ID`, `/deletesource ID`, `/queue`, `/collect`, `/stats`, `/auto_on`, `/auto_off`.

## Безопасность и стоимость
Cloud Run доступен публично для Telegram webhook, но проверяет секрет Telegram. Endpoint `/collect` защищён отдельным секретом в заголовке; доступ к конфигурации Scheduler ограничьте IAM. Текущая версия **не обеспечивает защиту от SSRF** при добавлении произвольных URL: подключайте только проверенные публичные RSS. Включите Billing Budget Alerts и квоты; budget alert не ограничивает расходы жёстко. Cloud Scheduler, Firestore, Secret Manager и AI могут стоить денег.

## Ограничения версии
MVP для RSS-новостей. Полноценная афиша с обязательными картой/контактами, SEO-сайт, мультимедиа, управление расписанием и категориями кнопками пока не реализованы. Новости Migri/Kela/законы направляются на модерацию. В случае статуса `needs_reconciliation` вручную проверить канал перед повторной отправкой.
