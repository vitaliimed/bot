"""Deterministic editing, language detection and conservative duplicate matching."""
import hashlib
import re
from difflib import SequenceMatcher
from .sources import clean


def language(item):
    known=item.get('source_language')
    if known in ('uk','fi','en','ru','sv'): return known
    text=clean(item.get('title','')+' '+item.get('summary',''),2500)
    if re.search('[іїєґІЇЄҐ]',text): return 'uk'
    try:
        from langdetect import detect,DetectorFactory
        DetectorFactory.seed=0
        return detect(text)
    except Exception: return 'unknown'


def excerpt(text,limit):
    value=clean(text,5000)
    if len(value)<=limit: return value
    cut=value[:limit-1]
    # A short source excerpt is retained verbatim; no invented summary or synonym rewriting.
    return cut.rsplit(' ',1)[0].rstrip()+ '…'


def prepare(item,cfg):
    lang=language(item)
    source=item.get('summary') or item.get('text') or ''
    text=excerpt(source,min(cfg['body_length'],450))
    joined=(item.get('title','')+' '+source).casefold()
    sensitive=item.get('manual_review',False) or any(word in joined for word in (
        'kela','migri','поліці','полиц','poliisi','лікуван','лечени','lääke','загину','загиб','kuoli','терор','теракт','turvapaik','закон'))
    target={'українська':'uk','suomi':'fi','русский':'ru','English':'en'}.get(cfg['language'],'uk')
    mismatch=lang!=target and cfg['text_mode']!='original'
    return dict(item,title=clean(item.get('title'),300),text=text,source_language=lang,
                text_method='original',ai_provider='',ai_model='',review_passed=False,
                translation_missing=mismatch,sensitive=sensitive or mismatch,
                missing_details=False,topic='',title_fi='',text_fi='',
                review_reason=('Перевод недоступен: сохранён оригинал. Нужна ручная проверка.' if mismatch else 'Обработано без ИИ'))


def fingerprint(item):
    text=clean(item.get('title','')+' '+(item.get('summary') or item.get('text') or ''),1500).casefold()
    return hashlib.sha256(re.sub(r'\W+',' ',text).strip().encode()).hexdigest()


def same_story(a,b):
    # Never merge factual details automatically. Flag a close match for human review.
    if a.get('category')!=b.get('category'): return False
    if a.get('event',{}).get('start')!=b.get('event',{}).get('start'): return False
    left=clean(a.get('title')).casefold();right=clean(b.get('title')).casefold()
    if len(left)<35 or len(right)<35: return False
    if re.findall(r'\d+',left)!=re.findall(r'\d+',right): return False
    return SequenceMatcher(None,left,right,autojunk=False).ratio()>=0.94
