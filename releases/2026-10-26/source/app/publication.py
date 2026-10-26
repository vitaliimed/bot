"""Publication identity and explicit manual-review snapshots."""
import hashlib
import json
from . import sources

MANUAL_STATUSES = ('pending','filtered','expired','rejected','awaiting_ai','queued','published','scheduled','duplicate')

def fingerprint(item):
    keys = ('title','text','title_fi','text_fi','summary','event','url','route','status','published_to','image_file_id','image_kind','delivery_id','send_target','claimed_at')
    value = {k:item.get(k) for k in keys}
    value['image_hash'] = hashlib.sha256(item.get('image_bytes') or b'').hexdigest()
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str,ensure_ascii=False).encode()).hexdigest()

def delivery_id(item, docid, target):
    value = sources.canonical(item['url']) if item.get('url') else docid
    value += '|'+str(item.get('event',{}).get('start',''))+'|'+target
    return hashlib.sha256(value.encode()).hexdigest()[:40]

def draft(item, target):
    result = dict(item,route=target)
    result['title'] = sources.clean(item.get('title') or 'Матеріал',300)
    result['text'] = '\n'.join(sources.clean(line,2500) for line in (item.get('text') or item.get('summary') or '').splitlines())[:2500]
    return result
