"""Restricted plain-text editorial templates; no executable expressions or raw HTML."""
from string import Formatter

FIELDS={'title_template':{'title','category'},'body_template':{'body'}}
REQUIRED={'title_template':'title','body_template':'body'}

def validate(key,value):
    fields=[]
    try:
        for _,name,spec,conversion in Formatter().parse(value):
            if name is None:continue
            if name not in FIELDS[key] or spec or conversion:raise ValueError()
            fields.append(name)
    except ValueError:raise ValueError('Только поля '+', '.join('{'+f+'}' for f in sorted(FIELDS[key]))+' без форматирующих выражений.')
    if REQUIRED[key] not in fields:raise ValueError('Обязательно оставьте {'+REQUIRED[key]+'}.')
    return value


def render(pattern,**values):
    return pattern.format_map(values)
