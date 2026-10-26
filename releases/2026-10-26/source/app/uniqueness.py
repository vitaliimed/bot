"""Conservative, channel-scoped exact story locks, additional to canonical URL locks."""
import hashlib
import re
import unicodedata
from datetime import timedelta
from .sources import clean


def normalize(value):
    return re.sub(r'\W+',' ',unicodedata.normalize('NFKC',clean(value or '',8000)).casefold()).strip()


def keys(item,route):
    title=normalize(item.get('title'));body=normalize(item.get('summary') or item.get('text'))
    event=str(item.get('event',{}).get('start') or '')
    values=[]
    # Short generic headings/texts must not block unrelated stories.
    if len(title)>=35:values.append('title|'+title+'|'+event)
    if len(body)>=100:values.append('body|'+body+'|'+event)
    return [hashlib.sha256((route+'|'+v).encode()).hexdigest()[:40] for v in values]


def blocked(record,current):
    if record.get('status') in ('sending','needs_reconciliation'):return True
    return record.get('status')=='published' and record.get('until',current)>current


def reservation(identity,route,current,hours,status='sending'):
    return {'item_id':identity,'route':route,'status':status,'at':current,'until':current+timedelta(hours=hours)}
