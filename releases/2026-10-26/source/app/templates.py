"""Local, deterministic branded JPEG cards. No AI or external image service."""
import io
import os
from PIL import Image,ImageDraw,ImageFont
from zoneinfo import ZoneInfo
from .sources import clean

PALETTES={'news':('#101C36','#4D97FF'),'sport':('#13332D','#73E2B1'),'events':('#302047','#F4B860')}
LABELS={'news':'НОВИНИ ФІНЛЯНДІЇ','sport':'СПОРТ','events':'ПОДІЇ · UUSIMAA'}

def font(size,bold=False):
    name='DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf'
    for root in ('/usr/share/fonts/truetype/dejavu','/usr/share/fonts/dejavu'):
        path=os.path.join(root,name)
        if os.path.exists(path): return ImageFont.truetype(path,size)
    raise RuntimeError('Template font missing; install fonts-dejavu-core')

def wrap(draw,text,face,width,max_lines):
    lines=[];line=''
    for word in text.split():
        while draw.textlength(word,font=face)>width:
            cut=len(word)-1
            while cut>1 and draw.textlength(word[:cut],font=face)>width: cut-=1
            if line: lines.append(line);line=''
            lines.append(word[:cut]);word=word[cut:]
        test=(line+' '+word).strip()
        if draw.textlength(test,font=face)>width:
            lines.append(line);line=word
        else: line=test
    if line: lines.append(line)
    if len(lines)>max_lines:
        lines=lines[:max_lines];last=lines[-1]
        while last and draw.textlength(last+'…',font=face)>width:last=last[:-1]
        lines[-1]=last+'…'
    return lines

def create(item,tag,style='auto',logo=None):
    category=style if style in PALETTES else 'events' if item.get('category')=='events' else 'sport' if item.get('category')=='sport' else 'news'
    background,accent=PALETTES[category]
    canvas=Image.new('RGB',(1200,675),background);draw=ImageDraw.Draw(canvas)
    draw.rounded_rectangle((45,42,1155,633),radius=24,outline=accent,width=2)
    draw.rounded_rectangle((75,77,87,121),radius=6,fill=accent)
    draw.text((108,80),LABELS[category],font=font(26,True),fill=accent)
    title=clean(item.get('title') or 'Фінляндія',300)
    size=54 if len(title)<85 else 44
    face=font(size,True)
    for n,line in enumerate(wrap(draw,title,face,1010,5)):
        draw.text((78,163+n*(size+12)),line,font=face,fill='white')
    event=item.get('event',{});parts=[]
    if event.get('start'):
        parts.append(event['start'].astimezone(ZoneInfo('Europe/Helsinki')).strftime('%d.%m.%Y' if event.get('date_only') else '%d.%m.%Y · %H:%M'))
    if event.get('city'):parts.append(clean(event['city'],40))
    if parts: draw.text((78,531),'  |  '.join(parts),font=font(25),fill=accent)
    draw.text((78,587),clean(tag,60),font=font(22,True),fill='white')
    draw.text((836,587),'ІНФОРМАЦІЙНА КАРТКА',font=font(17),fill='#B9C4D6')
    if logo:
        with Image.open(io.BytesIO(logo)) as original:
            mark=original.convert('RGBA');mark.thumbnail((115,60));canvas.paste(mark,(1020,66),mark)
    output=io.BytesIO();canvas.save(output,'JPEG',quality=86,optimize=True)
    return {'image_bytes':output.getvalue(),'image_kind':'template','image_provider':'local','image_error':''}
