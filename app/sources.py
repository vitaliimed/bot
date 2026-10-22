"""Bounded RSS and Linked Events adapters. Dates and locations are never invented by AI."""
import asyncio
import calendar
import hashlib
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qsl, urlencode
from zoneinfo import ZoneInfo
import feedparser
from bs4 import BeautifulSoup
from . import media


def clean(value, limit=4000):
    return BeautifulSoup(str(value or ''), 'html.parser').get_text(' ',strip=True)[:limit]


def localized(value):
    if isinstance(value,dict): return next((value[k] for k in ('uk','fi','en','sv') if value.get(k)), '')
    return value or ''


def date(value):
    if not value: return None
    try:
        d = datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return d.replace(tzinfo=ZoneInfo('Europe/Helsinki')) if not d.tzinfo else d
    except (TypeError, ValueError): return None


def canonical(url):
    p = urlsplit(url)
    query = [(k,v) for k,v in parse_qsl(p.query) if not k.lower().startswith('utm_') and k.lower() not in ('fbclid','gclid')]
    return urlunsplit((p.scheme,p.netloc.lower(),p.path.rstrip('/') or '/',urlencode(sorted(query)),''))


def identity(item):
    key = item['route']+'|'+canonical(item['url'])
    if item.get('event',{}).get('start'): key += '|'+item['event']['start'].isoformat()
    return hashlib.sha256(key.encode()).hexdigest()[:28]


def rss_entries(data, source, current):
    feed = feedparser.parse(data)
    if not feed.version: raise ValueError('Not an RSS/Atom feed')
    rows = []
    for entry in feed.entries[:30]:
        link = entry.get('link','')
        if urlparse(link).scheme != 'https' or not entry.get('title'): continue
        content = entry.get('content',[{}])[0].get('value') or entry.get('summary','')
        item = {'title':clean(entry.title,300),'summary':clean(content,5000),'url':link,
                'image_candidates':media.candidates(entry,link)}
        parsed = entry.get('published_parsed') or entry.get('updated_parsed')
        if parsed:
            try: item['source_published_at'] = datetime.fromtimestamp(calendar.timegm(parsed),timezone.utc)
            except (ValueError,OverflowError): pass
        # Community RSS often has announcements without machine-readable date/location.
        # They remain manual drafts until an administrator supplies verified event details.
        if source.get('route') == 'events': item['event'] = {'city':source.get('city','')}
        rows.append(item)
    return rows


def linked_entry(raw, source):
    place = raw.get('location') or {}
    if not isinstance(place,dict): place = {}
    offers = raw.get('offers') or []
    free = any(o.get('is_free') is True for o in offers)
    free = True if free else False if any(o.get('is_free') is False for o in offers) else None
    info = localized(raw.get('info_url'))
    # A real API permalink is preferable to a guessed public event page.
    url = info if str(info).startswith('https://') else raw.get('@id','')
    event = {'start':date(raw.get('start_time')), 'end':date(raw.get('end_time')),
             'date_only':raw.get('has_start_time') is False,
             'venue':clean(localized(place.get('name')),180),
             'city':clean(localized(place.get('address_locality')),80),
             'address':clean(localized(place.get('street_address')),180),
             'is_free':free,'price':clean(localized(next((o['price'] for o in offers if o.get('price')),None)),100),
             'languages':[str(v.get('@id','')).rstrip('/').split('/')[-1] for v in raw.get('in_language',[])],
             'cancelled':raw.get('deleted',False) or raw.get('event_status') in ('EventCancelled','EventPostponed'),
             'online':False, 'official_id':raw.get('id',''), 'api_url':raw.get('@id','')}
    images = [i for i in raw.get('images',[]) if i.get('url') and i.get('license') not in ('all_rights_reserved','no_publication')]
    return {'title':clean(localized(raw.get('name')),300),
            'summary':clean(localized(raw.get('description')) or localized(raw.get('short_description')),4500),
            'url':url,'event':event,'image_candidates':[i['url'] for i in images[:4]],
            'image_credit':clean(images[0].get('photographer_name',''),100) if images else '',
            'image_license':images[0].get('license','') if images else '',
            'source_published_at':date(raw.get('last_modified_time'))}


async def fetch(source, cfg, current):
    kind = source.get('kind','rss')
    if kind == 'rss':
        data,_ = await asyncio.wait_for(media.fetch(source['url'],limit=2_500_000,types=('application/xml','application/rss+xml','application/atom+xml','text/')),20)
        return rss_entries(data,source,current), {}
    if kind != 'linked': raise ValueError('Unsupported source kind')
    # Linked Events start/end filter overlapping events, not just future starts.
    # Filter future starts below; persist pagination so permanent exhibitions do not block the feed.
    page = max(1,int(source.get('next_page',1)))
    params = {'start':current.isoformat(),'end':(current+timedelta(days=cfg['event_horizon_days'])).isoformat(),
              'include':'location','sort':'end_time','recurring':'sub','page_size':50,'page':page}
    if source.get('division'): params['division'] = source['division']
    data,_ = await asyncio.wait_for(media.fetch(source['url']+'?'+urlencode(params),limit=4_000_000,types=('application/json',)),30)
    obj = json.loads(data)
    rows = [linked_entry(v,source) for v in obj.get('data',[])]
    rows = [v for v in rows if v['event'].get('start') and current < v['event']['start'] <= current+timedelta(days=cfg['event_horizon_days'])]
    return rows, {'next_page':page+1 if obj.get('meta',{}).get('next') else 1,
                  'upstream_count':obj.get('meta',{}).get('count',0)}


async def refresh_event(item):
    """Recheck a queued official event immediately before publication."""
    url = item.get('event',{}).get('api_url')
    if not url: return None
    detail_url = url + ('&' if '?' in url else '?') + 'include=location'
    data,_ = await asyncio.wait_for(media.fetch(detail_url,limit=200_000,types=('application/json',)),15)
    return linked_entry(json.loads(data),{})
