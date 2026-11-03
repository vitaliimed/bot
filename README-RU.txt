FINLAND BOT 2.0 — HOTFIX (24.10.2026)

Исправляет текущий код из репозитория vitaliimed/bot, коммит 34eb58c.

Что делает:
* Режим ai_mode=off без запросов к ИИ (оригинальный язык источника, без фальшивого перевода).
* Режим ai_mode=fallback (при сбое ИИ использует исходный текст).
* Опция no_ai_autopublish=on: разрешает автоматическую публикацию исходного текста новостей без ИИ, кроме простого набора тем с повышенным риском. По умолчанию off.
* mode=source_template: изображение источника или бесплатная карточка Pillow.
* mode=template: всегда локальная карточка.
* Очередь показывает сначала свежие записи, не выдаёт недатированные события за готовые.
* /incomplete показывает недатированные и некорректные события, не удаляя их.
* Ручная публикация сообщает причину отказа для события без даты и места.
* Создаёт резервные копии файлов в ~/projects/bot/backup-botfix-YYYYMMDD-HHMMSS.
* Коммит Git не изменяет. Данные Firestore и Scheduler не затрагивает.

В Cloud Shell:
1. Загрузите apply_botfix.py через меню Upload Cloud Shell в ~/projects/bot/.
2. cd ~/projects/bot
3. python3 apply_botfix.py
4. python3 -m compileall -q app
5. python3 -m unittest discover -s tests -v
6. PROJECT_ID=winged-ratio-377512 REGION=europe-north1 EVENTS_CHANNEL_ID=@eventhsl APP_VERSION_OVERRIDE=botfix-noai-v1 bash scripts/upgrade-all.sh

После обновления:
Отправьте @suominews_bot следующие команды (новости и события настраиваются отдельно):
/channel news
/set ai_mode off
/set no_ai_autopublish off
/set image_mode source_template
/set require_image on
/channel events
/set ai_mode off
/set image_mode source_template
/set require_image on
/incomplete
/queue

ВНИМАНИЕ: без ИИ нет автоматического перевода на украинский; исходный текст остаётся на языке источника. По умолчанию новости без ИИ требуют ручной модерации; для автоматической публикации новостей после проверки тестовых постов можно включить /set no_ai_autopublish on и /auto_on. Этот простой фильтр не заменяет редакторскую проверку. События проходят только при подтверждённых дате и месте. Не включайте автопубликацию событий до проверки черновиков. Для полной украинской локализации нужен ИИ или отдельный сервис перевода.

Для отката ДО РАЗВЁРТЫВАНИЯ: замените app/main.py, app/policy.py, app/media.py и scripts/upgrade-all.sh из указанной директории backup-botfix-... . Для отката после развёртывания Cloud Run см. команду в выводе upgrade-all.sh.
