# Finland News Bot — запуск в Google Cloud Shell

## Настройки запуска

- Проект Google Cloud: `winged-ratio-377512` с включённым billing.
- Регион Cloud Run: `europe-north1`.
- Регион Cloud Scheduler: `europe-west1`. В `europe-north1` планировщик недоступен; его регион задаётся отдельно через `SCHEDULER_LOCATION`.
- Канал: `@suominewsua`; бот `@suominews_bot` должен быть его администратором с правом публикации.
- Модель: `gemini-3.5-flash-lite`; доступность генерации и квоты зависят от проекта Gemini.
- `ADMIN_ID=0`: администратор пока не назначен. Управляющие команды, включая `/start`, не отвечают никому. Числовой Telegram ID можно указать позже.

Настройки можно переопределить переменными окружения. Токены и ключи в репозитории не хранятся.

## Запуск

```bash
cd "$HOME/projects/bot"
git pull --ff-only
bash scripts/launch-cloudshell.sh
```

Скрипт запросит токен Telegram-бота и ключ Gemini. Вставляйте значение и нажимайте Enter; символы при вводе не отображаются. Секреты сохраняются в Google Secret Manager.

После успешного развёртывания появится строка `Deployed: https://...`. Проверка сервиса:

```bash
URL=$(gcloud run services describe finland-news-bot \
  --project=winged-ratio-377512 --region=europe-north1 \
  --format='value(status.url)')
curl -fsS "$URL/health"
```

Ожидаемый ответ: `{"ok":true}`. Cloud Scheduler запускает сбор каждые 30 минут. Автопубликация в новой базе выключена: материалы остаются в очереди до назначения администратора и проверки.

При первом запуске Cloud Build может потребовать дополнительных IAM-разрешений. При ошибке сохраните последние строки вывода; не запускайте весь deploy повторно без проверки причины.

## Завершить настройку планировщика без повторного deploy

Если Cloud Run уже готов, а запуск остановился при создании Scheduler, выполните:

```bash
cd "$HOME/projects/bot"
git pull --ff-only
PROJECT_ID=winged-ratio-377512 \
  SCHEDULER_LOCATION=europe-west1 \
  bash scripts/configure-scheduler.sh
```

Скрипт прочитает существующий `cron-secret` из Secret Manager и создаст или обновит задание. Сервис Cloud Run и его секреты не изменяются. В результате ожидаются `state: ENABLED`, расписание `*/30 * * * *`, часовой пояс `Europe/Helsinki` и адрес с `/collect`. При обновлении существующего приостановленного задания оно остаётся приостановленным.

Для первого запуска сбора без ожидания расписания:

```bash
gcloud scheduler jobs run finland-news-collect \
  --project=winged-ratio-377512 --location=europe-west1
```

Это отправляет задание на выполнение. Результат обработки проверяется отдельно по `lastAttemptTime`, `status` задания и логам Cloud Run.

## Назначить администратора позже

Команда запросит числовой Telegram ID будущего администратора:

```bash
(
set -e
read -r -p 'Числовой Telegram ID администратора: ' NEW_ADMIN_ID
[[ "$NEW_ADMIN_ID" =~ ^[1-9][0-9]*$ ]] || { echo 'Нужен положительный числовой ID'; exit 1; }
gcloud run services update finland-news-bot \
  --project=winged-ratio-377512 --region=europe-north1 \
  --update-env-vars="ADMIN_ID=$NEW_ADMIN_ID"
)
```

После обновления назначенный администратор пишет боту `/start`, открывает `/sources`, запускает `/collect` и проверяет `/queue`. После проверки можно включить `/auto_on`. Текущая версия поддерживает одного администратора. При следующем запуске deploy укажите его ID через `ADMIN_ID`, чтобы не вернуть значение `0`.

## Ограничения версии

MVP для RSS-новостей: источники, очередь, редактирование и публикация через Telegram. Афиша с обязательными картой и контактами, SEO-сайт, мультимедиа и несколько администраторов пока не реализованы. Новости о Migri, Kela, законах и чувствительных темах направляются на модерацию. При статусе `needs_reconciliation` проверьте канал перед повторной отправкой.

Добавляйте только проверенные публичные RSS: полноценной защиты от SSRF пока нет. Доступ к заданию Scheduler должен быть ограничен IAM. Cloud Run, Scheduler, Firestore, Secret Manager и Gemini могут стоить денег; бюджетные уведомления не являются жёстким ограничителем расходов.
