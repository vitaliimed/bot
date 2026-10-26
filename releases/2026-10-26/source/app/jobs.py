"""Persistent, explicitly approved schedules and bounded bulk operations."""
import asyncio
import hashlib
import secrets
import time
from datetime import timedelta
from . import publication,policy,sources

async def schedule(bot,user,state):
    ref=bot.db.collection('items').document(state['id'])
    identity=hashlib.sha256((state['id']+state['target']+state['scheduled_at'].isoformat()).encode()).hexdigest()[:32]
    job=bot.db.collection('scheduled_posts').document(identity)
    @bot.firestore.transactional
    def save(tx):
        item=ref.get(transaction=tx).to_dict() or {}
        if publication.fingerprint(item)!=state['fingerprint']:return False
        if item.get('status') not in publication.MANUAL_STATUSES:return False
        item.update(status='scheduled',scheduled_at=state['scheduled_at'])
        tx.update(ref,{'status':'scheduled','scheduled_at':state['scheduled_at']})
        tx.set(job,{'item_id':state['id'],'route':state['target'],'at':state['scheduled_at'],'actor':user,
                    'status':'scheduled','fingerprint':publication.fingerprint(item),'config':state['config'],'created_at':bot.now()})
        return True
    return await asyncio.to_thread(save,bot.db.transaction())

async def run_due(bot,route):
    active=await bot.documents(bot.db.collection('scheduled_posts').where('status','==','scheduled'))
    active+=await bot.documents(bot.db.collection('scheduled_posts').where('status','==','running'))
    jobs=[d for d in active if d.to_dict().get('route')==route and
          d.to_dict().get('status') in ('scheduled','running') and d.to_dict()['at']<=bot.now()]
    jobs.sort(key=lambda d:d.to_dict()['at'])
    cfg=await bot.editor_config(route)
    done=0
    deadline=time.monotonic()+45
    for doc in jobs[:cfg['dispatch_batch_size']]:
        if time.monotonic()>deadline:break
        job=doc.to_dict();ref=bot.db.collection('items').document(job['item_id'])
        item=(await asyncio.to_thread(ref.get)).to_dict() or {}
        if route in bot.delivery_history(item):
            await asyncio.to_thread(doc.reference.update,{'status':'published'});continue
        if job['status']=='running' and job.get('claimed_at',bot.now())>bot.now()-timedelta(minutes=10):continue
        if not await bot.admin_access(job['actor']) or not await bot.admin_access(job['actor'],route):
            await asyncio.to_thread(doc.reference.update,{'status':'failed','reason':'У автора задания больше нет прав публикации'});continue
        if publication.fingerprint(item)!=job['fingerprint']:
            await asyncio.to_thread(doc.reference.update,{'status':'failed','reason':'Карточка изменена или отправка не подтверждена'});continue
        if policy.event_expired(item,bot.now()) or item.get('event',{}).get('cancelled'):
            await asyncio.to_thread(doc.reference.update,{'status':'failed','reason':'Событие истекло или отменено'});continue
        if item.get('event',{}).get('api_url'):
            try:
                fresh=await sources.refresh_event(item)
                if fresh and fresh['event']!=item.get('event'):raise ValueError('Event changed')
            except Exception:
                await asyncio.to_thread(doc.reference.update,{'status':'failed','reason':'Источник изменился или недоступен; нужна проверка'});continue
        if job.get('batch_id') and await bot.editor_config(route)!=job['config']:
            await asyncio.to_thread(doc.reference.update,{'status':'failed','reason':'Настройки изменены после подтверждения; проверьте карточку'})
            continue
        @bot.firestore.transactional
        def claim(tx):
            current=doc.reference.get(transaction=tx).to_dict() or {}
            if current.get('status') not in ('scheduled','running'):return False
            if current.get('status')=='running' and current.get('claimed_at',bot.now())>bot.now()-timedelta(minutes=10):return False
            tx.update(doc.reference,{'status':'running','claimed_at':bot.now()})
            return True
        if not await asyncio.to_thread(claim,bot.db.transaction()):continue
        try:
            success=await bot.publish(job['item_id'],target_route=route,cfg=job['config'],override=True,expected_fingerprint=job['fingerprint'])
        except Exception as error:
            await asyncio.to_thread(doc.reference.update,{'status':'failed','reason':'Ошибка отправки: '+type(error).__name__+'; проверьте статус материала перед повтором'})
            continue
        await asyncio.to_thread(doc.reference.update,{'status':'published' if success else 'failed','reason':'' if success else 'Проверьте статус материала'})
        done+=int(success)
    return done

async def select(bot,chat,user,docid):
    if not await bot.authorize_item(user,docid):return await bot.say(chat,'Нет доступа к материалу.')
    ids=await bot.setting(f'selection:{user}',[])
    if docid in ids:ids.remove(docid)
    elif len(ids)<10:ids.append(docid)
    else:return await bot.say(chat,'В одном групповом действии не более 10 материалов.')
    await bot.put_setting(f'selection:{user}',ids)
    return await show(bot,chat,user)

async def show(bot,chat,user):
    ids=await bot.setting(f'selection:{user}',[])
    lines=['Выбрано: '+str(len(ids))]
    for identity in ids:
        if not await bot.authorize_item(user,identity):continue
        item=(await asyncio.to_thread(bot.db.collection('items').document(identity).get)).to_dict() or {}
        lines.append(identity+' · '+item.get('title','')[:90])
    rows=[[{'text':'Повторить обработку','callback_data':'bulk:retry:_'},{'text':'Отклонить выбранные','callback_data':'bulk:reject:_'}]]
    for route,value in bot.CHANNELS.items():
        if value.get('enabled',True) and await bot.admin_access(user,route):
            rows.append([{'text':'Публиковать в '+bot.target_channel(route),'callback_data':'bulk:publish:'+route}])
    rows.append([{'text':'Снять выделение','callback_data':'bulkclear'},{'text':'Назад','callback_data':'unpublished:all:0'}])
    return await bot.panel(chat,'\n'.join(lines),rows)

async def prepare_bulk(bot,chat,user,action,target):
    ids=await bot.setting(f'selection:{user}',[])
    if not ids:return await bot.say(chat,'Сначала выберите материалы кнопкой «В группу» в карточках.')
    if action not in ('retry','reject','publish'):return
    if action=='publish' and (target not in bot.CHANNELS or not await bot.admin_access(user,target)):return await bot.say(chat,'Нет доступа к каналу.')
    values=[]
    cfg=await bot.editor_config(target if action=='publish' else None)
    for identity in ids:
        if not await bot.authorize_item(user,identity):continue
        item=(await asyncio.to_thread(bot.db.collection('items').document(identity).get)).to_dict() or {}
        if item.get('status') not in publication.MANUAL_STATUSES:continue
        if action=='publish':
            if target in bot.delivery_history(item) or (item.get('import_error') and not item.get('text')):continue
            await bot.send_item(chat,publication.draft(item,target),cfg)
        values.append({'id':identity,'fingerprint':publication.fingerprint(item)})
    if not values:return await bot.say(chat,'Нет подходящих материалов для выбранного действия.')
    nonce=secrets.token_hex(8)
    await bot.put_setting(f'bulk:{user}',{'action':action,'target':target,'items':values,'config':cfg,'nonce':nonce,'expires':bot.now()+timedelta(minutes=10)})
    return await bot.say(chat,f'Подтвердить: {action}, материалов {len(values)}'+(' в '+bot.target_channel(target) if action=='publish' else '')+
        '? Публикация обходит автоматические фильтры и лимиты. Проверьте все превью выше.',[
        [{'text':'Подтвердить групповое действие','callback_data':'bulkconfirm:'+nonce}],[{'text':'Отмена','callback_data':'bulkcancel'}]])

async def confirm_bulk(bot,chat,user,nonce):
    state=await bot.setting(f'bulk:{user}')
    if not isinstance(state,dict) or state.get('nonce')!=nonce or state.get('expires',bot.now())<=bot.now():return await bot.say(chat,'Подтверждение истекло.')
    await bot.put_setting(f'bulk:{user}',None)
    done=0
    for value in state['items']:
        identity=value['id']
        if not await bot.authorize_item(user,identity):continue
        ref=bot.db.collection('items').document(identity)
        item=(await asyncio.to_thread(ref.get)).to_dict() or {}
        if publication.fingerprint(item)!=value['fingerprint']:continue
        if state['action']=='publish':
            if not await bot.admin_access(user,state['target']):continue
            done+=int(await bot.publish(identity,target_route=state['target'],cfg=state['config'],override=True,expected_fingerprint=value['fingerprint']))
        elif state['action']=='retry':
            done+=int(await bot.transition_item(ref,{'status':'queued','attempts':0},tuple(s for s in publication.MANUAL_STATUSES if s!='published')))
        else:
            done+=int(await bot.transition_item(ref,{'status':'rejected','reason':'Отклонено редактором, можно восстановить'},tuple(s for s in publication.MANUAL_STATUSES if s!='published')))
    await bot.put_setting(f'selection:{user}',[])
    return await bot.say(chat,f'Выполнено: {done} из {len(state["items"])}. Пропущенные карточки могли измениться или уже быть опубликованы; откройте их статус.')
