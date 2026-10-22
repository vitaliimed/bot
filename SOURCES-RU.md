# Источники бота

Каталог проверялся при подготовке 21–22 октября 2026 года. Доступность сторонних сайтов может меняться.

## Новости: 27 RSS-лент

Тематические ленты одной редакции показаны отдельно. Количество лент не означает количество независимых СМИ.

| ID | Источник | URL |
|---|---|---|
| `yle-news-en` | Yle News | https://yle.fi/rss/news |
| `yle-uk` | Новини Yle | https://yle.fi/rss/novyny |
| `yle-fi` | Yle Uutiset | Tuoreimmat | https://yle.fi/rss/uutiset/tuoreimmat |
| `yle-simple` | Yle Uutiset selkosuomeksi | https://yle.fi/rss/selkouutiset |
| `yle-uusimaa` | Yle.fi | Uusimaa | https://yle.fi/rss/t/18-147345/fi |
| `yle-economy` | Yle.fi | Talous | https://yle.fi/rss/t/18-19274/fi |
| `yle-culture` | Yle.fi | Kulttuuri | https://yle.fi/rss/t/18-150067/fi |
| `yle-health` | Yle.fi | Terveys | https://yle.fi/rss/t/18-35138/fi |
| `yle-transport` | Yle.fi | Liikenne | https://yle.fi/rss/t/18-12/fi |
| `yle-science` | Yle.fi | Tiede | https://yle.fi/rss/t/18-819/fi |
| `yle-sports` | Yle Urheilu | Tuoreimmat | https://yle.fi/rss/urheilu |
| `hs-finland` | Suomi - Helsingin Sanomat | https://www.hs.fi/rss/suomi.xml |
| `hs-helsinki` | Helsinki - Helsingin Sanomat | https://www.hs.fi/rss/helsinki.xml |
| `hs-economy` | Talous - Helsingin Sanomat | https://www.hs.fi/rss/talous.xml |
| `hs-children` | Lasten uutiset - Helsingin Sanomat | https://www.hs.fi/rss/lastenuutiset.xml |
| `hs-science` | Tiede - Helsingin Sanomat | https://www.hs.fi/rss/tiede.xml |
| `is-finland` | Kotimaa - Ilta-Sanomat | https://www.is.fi/rss/kotimaa.xml |
| `is-economy` | Taloussanomat - Ilta-Sanomat | https://www.is.fi/rss/taloussanomat.xml |
| `is-family` | Perhe - Ilta-Sanomat | https://www.is.fi/rss/perhe.xml |
| `is-digital` | Digitoday - Ilta-Sanomat | https://www.is.fi/rss/digitoday.xml |
| `il-news` | Iltalehti.fi tuoreimmat uutiset - Uutiset | https://www.iltalehti.fi/rss/uutiset.xml |
| `il-money` | Iltalehti.fi tuoreimmat uutiset - Raha | https://www.iltalehti.fi/rss/raha.xml |
| `il-family` | Iltalehti.fi tuoreimmat uutiset - Perhe | https://www.iltalehti.fi/rss/perhe.xml |
| `helsinki-times` | Helsinki Times - Helsinki Times | https://www.helsinkitimes.fi/?format=feed&type=rss |
| `finland-today` | Finland Today | News in English | finlandtoday.fi | https://finlandtoday.fi/feed/ |
| `ukrainians` | Асоціація українців у Фінляндії | https://ukrainians.fi/uk/feed/ |
| `traficom` | www.traficom.fi RSS | https://www.traficom.fi/feed/rss/fi |

## События

| ID | Источник | Тип |
|---|---|---|
| `events-helsinki` | [Helsinki — Linked Events](https://api.hel.fi/linkedevents/v1/event/) | linked |
| `events-espoo` | [Espoo — город, библиотеки, музеи и организаторы](https://api.espoo.fi/events/v1/event/) | linked |
| `events-vantaa` | [Vantaa — события в Linked Events](https://api.hel.fi/linkedevents/v1/event/) | linked |
| `events-ukrainians` | [Асоціація українців у Фінляндії](https://ukrainians.fi/uk/feed/) | rss |
| `events-proukraina` | [Pro Ukraina](https://www.proukraina.fi/feed/) | rss |

Helsinki и Espoo агрегируют данные разных городских подразделений и организаторов: библиотеки, музеи, культурные центры и другие площадки. Это два API, а не отдельный API каждого организатора. Выборка Вантаа охватывает только записи, опубликованные в Linked Events; полный городской календарь Eventz/Townbase требует отдельного подключения.

RSS украинских организаций может содержать обычные новости и отчёты. Бот фильтрует их; анонс без подтверждённой даты/места требует ручного заполнения через `/event`. Не обещается полное покрытие всех мероприятий или закрытых сообществ.

Ссылки на справочники: [Yle RSS](https://yle.fi/rss), [HS RSS](https://www.hs.fi/rss/), [IS RSS](https://www.is.fi/rss/), [Iltalehti RSS](https://www.iltalehti.fi/rss), [Traficom RSS](https://www.traficom.fi/fi/uutishuone/tilaa-traficomin-uutiset-rss-syotteina), [Helsinki Linked Events](https://github.com/City-of-Helsinki/linkedevents), [Espoo Events](https://hri.fi/data/en/dataset/espoon-tapahtumarajapinta).
