# Finland Bot — AI provider connection and source audit (25.10.2026)

## Цель

Подтвердить работу Cloudflare Workers AI и Haimaker с текущими API, не публикуя материалы в Telegram и не изменяя Cloud Run/Firestore/GitHub. Если Haimaker работает, сохранить его отдельный API-ключ в Secret Manager. Подтвердить подлинность исходников работающей версии для следующего этапа обновления кода.

## Шаг 1. Распаковка

```bash
cd "$HOME/projects/bot"
unzip -o finland-ai-connect-pack-20261025.zip -d finland-ai-connect-pack
```

Оригинальный файл `finland_all_ai_test_20261025.py` должен находиться рядом, в `~/projects/bot`: его безопасные функции используются для чтения существующего Cloudflare токена из ссылки на Secret Manager. Если он не найден, тест Cloudflare пропускается.

## Шаг 2. Тест Cloudflare и добавление ключа Haimaker

```bash
cd "$HOME/projects/bot"
python3 finland-ai-connect-pack/finland_ai_setup_20261025.py \
  --save-haimaker-key --test-images
```

Скрипт попросит скрытым вводом новый ключ Haimaker **All Models**. Если Haimaker API вернёт корректный украинский перевод через `z-ai/glm-5.3`, скрипт сохранит ключ в Secret Manager под именем `finland-bot-haimaker-api-key`. Cloudflare тестирует текст и 1 изображение FLUX на указанном Account ID. Ключи не выводятся, тестовые посты не публикуются. Использование API может расходовать квоту. Haimaker бесплатная акция закончится 1 ноября 2026 в 14:00 UTC; после этой даты этот скрипт запрещает запросы без отдельного ключа `--allow-paid-haimaker-test`.

Если Haimaker ключ уже лежит в этом Secret Manager, допускается повторный запуск без `--save-haimaker-key`.

Результат сохраняется под `~/bot-ai-audits/connect-.../report.json`; тестовое изображение — в той же папке.

## Шаг 3. Проверить исходники, действительно запущенные в Cloud Run

```bash
python3 finland-ai-connect-pack/finland_capture_live_source_20261025.py
```

Скрипт сопоставляет digest контейнера **активной** Cloud Run revision с digest результата Cloud Build. Только при **однозначном** совпадении скачивает исходники сборки в новую папку `~/bot-live-source-*` и формирует `review-source.zip`. Он не перезаписывает `~/projects/bot`, не обновляет Git и не развёртывает старый код. Если точное совпадение подтвердить нельзя, операция останавливается.

Проверьте `review-source.zip` на случайно сохранённые в исходниках секреты перед передачей в ChatGPT. Никогда не передавайте `.env`, токены, личные ключи или бэкапы Firestore.

## После проверки

Получение двух API-ответов — не то же самое, что встраивание провайдеров в бот. Для добавления Haimaker девятым провайдером, исправления `cost_mode=free`, проверки украинского текста перед публикацией, использования FLUX после отсутствующего исходного изображения, и переключения очередей потребуется адаптировать настоящий исходный код Cloud Run и безопасно развернуть новую ревизию с проверкой до перевода трафика. Этот пакет **не меняет поведение работающего бота**.

Документация:
- Haimaker: https://docs.haimaker.ai/docs
- Бесплатная акция: https://haimaker.ai/free/
- Cloudflare FLUX: https://developers.cloudflare.com/workers-ai/models/flux-1-schnell/
- Cloudflare pricing: https://developers.cloudflare.com/workers-ai/platform/pricing/
