"""Public event pages and ticket listings. Unstructured imports always require review."""
import asyncio
import html
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from . import media
from .sources import clean, date


def public_url(value, base=''):
    url = urljoin(base,str(value or ''))
    p = urlparse(url)
    return url if p.scheme=='https' and p.hostname and not (p.username or p.password) else ''


def walk(value):
    if isinstance(value,dict):
        yield value
        for child in value.values(): yield from walk(child)
    elif isinstance(value,list):
        for child in value: yield from walk(child)


def page_item(data, url):
    soup = BeautifulSoup(data,'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        try: values = json.loads(script.get_text())
        except (ValueError,TypeError): continue
        for raw in walk(values):
            types = raw.get('@type',[])
            if isinstance(types,str): types=[types]
            if not any(str(t).split('/')[-1].endswith('Event') for t in types) or not raw.get('name'): continue
            location = raw.get('location') or {}
            if isinstance(location,list): location=location[0] if location else {}
            if not isinstance(location,dict): location={'name':location}
            address = location.get('address') or {}
            if not isinstance(address,dict): address={'streetAddress':address}
            offers = raw.get('offers') or {}
            if isinstance(offers,list): offers=offers[0] if offers else {}
            if not isinstance(offers,dict): offers={}
            images = raw.get('image') or []
            if not isinstance(images,list): images=[images]
            return {'title':clean(raw['name'],300),'summary':clean(raw.get('description'),5000),'url':url,
                    'image_candidates':[u for v in images if (u:=public_url(v.get('url') if isinstance(v,dict) else v,url))],
                    'manual_review':True,
                    'event':{'start':date(raw.get('startDate')),'end':date(raw.get('endDate')),
                             'date_only':len(str(raw.get('startDate','')))==10,'venue':clean(location.get('name'),180),
                             'address':clean(address.get('streetAddress'),180),'city':clean(address.get('addressLocality'),80),
                             'price':clean(str(offers.get('price',''))+' '+str(offers.get('priceCurrency','')),100),
                             'ticket_url':public_url(offers.get('url'),url) if offers.get('url') else '',
                             'cancelled':str(raw.get('eventStatus','')).endswith(('EventCancelled','EventPostponed')),
                             'online':str(location.get('@type','')).endswith('VirtualLocation')}}
    title = soup.select_one('meta[property="og:title"]')
    description = soup.select_one('meta[property="og:description"], meta[name="description"]')
    body = soup.select_one('article, main')
    for node in soup.select('script,style,nav,header,footer,form'): node.decompose()
    return {'title':clean(title.get('content') if title else soup.title.get_text() if soup.title else urlparse(url).hostname,300),
            'summary':clean(body.get_text(' ',strip=True) if body else description.get('content') if description else '',5000),
            'url':url,'event':{},'image_candidates':media.page_images(data,url),'manual_review':True}


async def import_url(url):
    host = (urlparse(url).hostname or '').lower()
    if host=='facebook.com' or host.endswith('.facebook.com') or host=='fb.me':
        return {'url':url,'title':'Событие из Facebook','summary':'','event':{},'image_candidates':[],
                'manual_review':True,'import_error':'Добавьте текст анонса. Бот не читает закрытые страницы Facebook.'}
    data,resolved = await asyncio.wait_for(media.fetch(url,limit=2_500_000,types=('text/html',)),20)
    return await asyncio.to_thread(page_item,data,resolved)


def pro_ukraina(data, source, cfg, current):
    soup = BeautifulSoup(data,'html.parser')
    rows=[]
    cities = [v.strip() for v in cfg['event_cities'].split(',') if v.strip()]
    for li in soup.select('li'):
        text=li.get_text(' ',strip=True)
        match=re.match(r'^[A-Za-z]{2} (\d{1,2})\.(\d{1,2})\.(\d{4}),',text)
        if not match: continue  # Ranges/exhibitions require a separate verified occurrence.
        clock=re.search(r'\bklo\s*(\d{1,2})[:.](\d{2})',text)
        try:
            day,month,year=map(int,match.groups())
            start=datetime(year,month,day,int(clock[1]) if clock else 0,int(clock[2]) if clock else 0,tzinfo=ZoneInfo('Europe/Helsinki'))
        except ValueError: continue
        if start.date()<current.astimezone(ZoneInfo('Europe/Helsinki')).date() or start>current+timedelta(days=cfg['event_horizon_days']): continue
        city=next((v for v in cities if re.search(r'\b'+re.escape(v)+r'\b',text,re.I)),'')
        if cities and not city: continue
        links=li.select('a[href]'); title=li.select_one('strong')
        url=next((public_url(a.get('href'),source['url']) for a in reversed(links) if a.get_text(strip=True).casefold()=='sivusto'),source['url'])
        icons=' '.join(i.get('src','') for i in li.select('img'))
        free=False if 'NON-FREE' in icons else True if '/FREE' in icons else None
        rows.append({'title':clean(title.get_text() if title else text,300),'summary':clean(text,3000),'url':url,
            'manual_review':True,'image_candidates':[],'source_page':source['url'],
            'event':{'start':start,'date_only':clock is None,'city':city,'venue':clean(links[0].get_text() if links else '',180),
                     'is_free':free,'languages':['uk'] if 'Flag_of_Ukraine' in icons else [],'online':False,
                     'ticket_url':url if urlparse(url).hostname in ('fienta.com','www.tiketti.fi','www.lippu.fi','www.netticket.fi') else ''}})
    return rows


def tiketti(data, source, cfg, current):
    obj=json.loads(data)
    if not isinstance(obj.get('keys'),list) or not isinstance(obj.get('events'),list): raise ValueError('Tiketti listing format changed')
    rows=[]; cities={v.strip().casefold() for v in cfg['event_cities'].split(',') if v.strip()}
    for raw in obj['events']:
        v=dict(zip(obj['keys'],raw))
        location=obj.get('locations',{}).get(str(v.get('locationID')),{})
        city=str(location.get('city',''))
        if cities and city.casefold() not in cities: continue
        start=date(v.get('start_date'))
        if not start or start.date()<current.astimezone(ZoneInfo('Europe/Helsinki')).date() or start>current+timedelta(days=cfg['event_horizon_days']): continue
        try: flags=int(v.get('flags') or 0)
        except ValueError: continue
        if flags&4 or not str(v.get('eventID','')).isdigit() or not v.get('url'): continue
        url=public_url(v['url'],'https://www.tiketti.fi/')
        summary=clean(str(v.get('texts','')),4500)
        rows.append({'title':clean(v.get('name'),300),'summary':summary+'\n'+clean(v.get('timeinfo'),150),
            'url':url,'image_candidates':[v['img_url']] if public_url(v.get('img_url')) else [],
            'manual_review':True,'event':{'start':start,'date_only':True,'city':city,'venue':clean(location.get('name'),180),
                'price':clean(html.unescape(str(v.get('priceinfo',''))),100),'ticket_url':url,'cancelled':False,
                'sold_out':bool(flags&8),'time_note':clean(v.get('timeinfo'),100),'age_limit':clean(v.get('ageinfo'),40),'organizer':clean(v.get('organizer'),150)}})
    rows.sort(key=lambda v:(v['event']['start'],v['url']))
    page=max(1,int(source.get('next_page',1)))
    if (page-1)*50>=len(rows): page=1
    return rows[(page-1)*50:page*50],{'next_page':page+1 if page*50<len(rows) else 1,'upstream_count':len(rows)}
