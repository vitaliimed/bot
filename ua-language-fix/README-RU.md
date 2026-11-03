# Защита от финских текстов при публикации на украинском

## Что делает

- Перед вызовом существующего `publish(docid,...)` проверяет реальную настройку языка данного канала.
- Если язык `українська`, но описание финское/английское, не даёт опубликовать его.
- По умолчанию `UA_TRANSLATION_MODE=off`: не делает платных вызовов и ничего не публикует на другом языке.
- Если явно установить `UA_TRANSLATION_MODE=gemini` и в Cloud Run доступен `GEMINI_API_KEY`, переводит, проверяет и сохраняет результат; при ошибке не публикует.
- Для перевода использует модель из настройки `gemini_model` или переменной `UA_TRANSLATION_MODEL`, если задана.
- Не трогает историю публикаций, лимиты, расписание, Firestore базы, исходные записи и GitHub.
- НЕ переводит уже опубликованные посты в Telegram задним числом.
- НЕ исправляет саму цепочку редактора/генерации; это защитный слой непосредственно перед публикацией.

## Ограничения и предосторожности

Это hotfix для кода с `async publish(docid...)`, `route_of`, `editor_config` и глобальной Firestore `db`. Если версия Cloud Run отличается, `--check` остановится. В других путях отправки, которые не вызывают `publish`, защита НЕ действует. Анализировать сначала на подготовленной копии v2. Правило определения языка основано на письме (Unicode), оно консервативно: если перевод вызывает сомнения, материал останется неопубликованным. Не заменяет точное определение языка моделью.

При включении Gemini проверьте тариф и бесплатную квоту. API может тарифицироваться; hotfix не добавляет общего распределенного суточного лимита вызовов. Рекомендуется вначале оставить перевод выключенным, убедиться в блокировке финского, затем настроить перевод/лимиты в полном редакторе.

## Установка на исходники развернутой v2 (Cloud Shell)

**Быстрый вариант**: загрузите ZIP в `$HOME/projects/bot/`, затем запустите:

```bash
cd "$HOME/projects/bot"
unzip -q -o ua-bot-language-fix-20261025.zip -d ua-language-fix
bash ua-language-fix/install-cloudshell.sh
```

1. Сохраните данный ZIP в `$HOME/projects/bot/ua-bot-language-fix-20261025.zip` (через Upload в Cloud Shell).
2. Выполните команду ниже (важно: использовать v2-архив, из которого запускался Cloud Run, а НЕ старый GitHub main).

```bash
(
set -euo pipefail
BASE="$HOME/projects/bot"
mkdir -p "$BASE"
V2="$BASE/finland-news-bot-v2-20261025.zip"
FIX="$BASE/ua-bot-language-fix-20261025.zip"
test -f "$V2" || { echo "ОШИБКА: нет исходного v2 ZIP: $V2"; exit 1; }
test -f "$FIX" || { echo "ОШИБКА: нет исправления: $FIX"; exit 1; }
BUILD=$(mktemp -d "$BASE/ua-lang-v2-XXXXXXXX")
unzip -q "$V2" -d "$BUILD"
SRC="$BUILD/finland-news-bot-v2"
test -d "$SRC/app" || { echo "Не обнаружена ожидаемая папка v2/app"; exit 1; }
unzip -q "$FIX" -d "$SRC"
cd "$SRC"
python3 apply_ua_language_fix.py --check
python3 -m unittest discover -s tests -p 'test_ua_language_guard.py' -v
python3 apply_ua_language_fix.py --apply
python3 -m compileall -q app
python3 -m unittest discover -s tests -v
# Только после прохождения всех проверок развернуть (сохранённые настройки бота не перезаписывать)
PROJECT_ID=winged-ratio-377512 REGION=europe-north1 EVENTS_CHANNEL_ID=@eventhsl bash scripts/upgrade-all.sh
bash scripts/diagnose.sh
)
```


## Включить перевод (дополнительная опция, не по умолчанию)

При активном Gemini API ключе и после проверки расходов:

```bash
gcloud run services update finland-news-bot \
  --project=winged-ratio-377512 --region=europe-north1 \
  --update-env-vars=UA_TRANSLATION_MODE=gemini
```

Обратите внимание: команда обновляет ревизию Cloud Run. Для запрета платных вызовов установите `UA_TRANSLATION_MODE=off` через `--update-env-vars`.

## Откат

При установке создаётся `app/main.py.pre-ua-guard-<timestamp>.bak` в папке сборки. Чтобы вернуть исходный сервис без пересборки, используйте историю ревизий Cloud Run и перенаправьте 100% трафика на предыдущую проверенную ревизию. Не выполняйте массовую публикацию 451 записей без проверки дат и дубликатов.
