"""Confirmed bulk publication backed by persistent scheduled_posts, never a webhook loop."""
import asyncio
import secrets
from datetime import timedelta
from . import publication, policy

async def menu(bot, chat):
    return await bot.panel(chat, '📣 Массовая публикация\nВыберите количество подготовленных материалов текущего канала. Сначала будет список и выбор назначения. До подтверждения ничего не отправляется.', [
        [{'text':str(n)+' материалов','callback_data':'masspick:'+str(n)} for n in (10,25)],
        [{'text':str(n)+' материалов','callback_data':'masspick:'+str(n)} for n in (50,100)],
        [{'text':'Статус / отмена заданий','callback_data':'massstatus'}],
        [{'text':'Выбрать вручную','callback_data':'unpublished:all:0'},{'text':'Назад','callback_data':'home'}]])

async def pick(bot,chat,user,count):
    if count not in (10,25,50,100):return
    route=bot.ROUTE.get()
    docs=[d for d in await bot.item_metadata() if bot.route_of(d.to_dict())==route and d.to_dict().get('status')=='pending']
    docs.sort(key=lambda d:str(d.to_dict().get('created_at','')),reverse=True)
    ids=[d.id for d in docs[:count]]
    if not ids:return await bot.say(chat,'Нет подготовленных материалов. Откройте очередь → повторить обработку или создайте пост без ИИ.')
    await bot.put_setting(f'masspick:{user}',{'ids':ids,'expires':bot.now()+timedelta(minutes=10)})
    rows=[]
    for target,value in bot.CHANNELS.items():
        if value.get('enabled',True) and await bot.admin_access(user,target):
            rows.append([{'text':bot.target_channel(target),'callback_data':'masstarget:'+target}])
    return await bot.panel(chat,f'Выбрано до {len(ids)} свежих подготовленных материалов. Куда публиковать?',rows)

async def preview(bot,chat,user,target):
    state=await bot.setting(f'masspick:{user}')
    if not isinstance(state,dict) or state.get('expires',bot.now())<=bot.now():return await bot.say(chat,'Выбор истёк. Откройте массовую публикацию снова.')
    if target not in bot.CHANNELS or not await bot.admin_access(user,target):return await bot.say(chat,'Нет доступа к каналу.')
    cfg=await bot.editor_config(target);values=[];skipped=0
    ledger={}
    for identity in state['ids'][:100]:
        if not await bot.authorize_item(user,identity):skipped+=1;continue
        item=(await asyncio.to_thread(bot.db.collection('items').document(identity).get)).to_dict() or {}
        if item.get('status')!='pending' or target in bot.delivery_history(item) or policy.event_reason(item,bot.now()):skipped+=1;continue
        reason=policy.automatic_reason(item,cfg,bot.now(),ledger)
        values.append({'id':identity,'fingerprint':publication.fingerprint(item),'title':item.get('title','Без заголовка')[:100], 'warning':reason})
    if not values:return await bot.say(chat,'Нет подходящих материалов: проверьте даты, статусы и историю публикаций.')
    nonce=secrets.token_hex(8)
    await bot.put_setting(f'mass:{user}',{'items':values,'target':target,'config':cfg,'nonce':nonce,'expires':bot.now()+timedelta(minutes=15)})
    for offset in range(0,len(values),15):
        chunk=values[offset:offset+15]
        text='\n'.join(f'{offset+i+1}. {v["title"]}'+ ('\n⚠️ '+v['warning'] if v['warning'] else '') for i,v in enumerate(chunk))
        rows=[[{'text':'Просмотр '+str(offset+i+1),'callback_data':'massview:'+str(offset+i)}] for i,v in enumerate(chunk)]
        await bot.say(chat,text,rows)
    return await bot.say(chat,f'В канал {bot.target_channel(target)}: {len(values)} материалов; исключено {skipped}.\nРучное подтверждение обходит автоматическую модерацию, частоту и дневные лимиты. События с неверной датой не отправляются. Задания выполняются отдельным обработчиком каждые 2 минуты; количество за запуск задаётся в настройках. Проверьте предупреждения и карточки выше.',[
        [{'text':'Подтвердить массовую публикацию','callback_data':'massconfirm:'+nonce}],
        [{'text':'Отмена','callback_data':'massdiscard'}]])

async def confirm(bot,chat,user,nonce):
    ref=bot.db.collection('settings').document(f'mass:{user}')
    state=await bot.setting(f'mass:{user}')
    if not isinstance(state,dict) or state.get('nonce')!=nonce or state.get('expires',bot.now())<=bot.now():return await bot.say(chat,'Подтверждение истекло или уже использовано.')
    target=state['target']
    if not await bot.admin_access(user,target):return await bot.say(chat,'Нет прав публикации.')
    if await bot.editor_config(target)!=state['config']:return await bot.say(chat,'Настройки изменились. Выберите список заново.')
    allowed=[]
    for value in state['items']:
        if await bot.authorize_item(user,value['id']):allowed.append(value)
    @bot.firestore.transactional
    def save(tx):
        current=(ref.get(transaction=tx).to_dict() or {}).get('value')
        if not isinstance(current,dict) or current.get('nonce')!=nonce or current.get('expires',bot.now())<=bot.now():return 0
        reads=[(v,bot.db.collection('items').document(v['id'])) for v in allowed]
        snapshots=[(v,r,r.get(transaction=tx).to_dict() or {}) for v,r in reads]
        count=0
        for value,itemref,item in snapshots:
            if item.get('status')!='pending' or publication.fingerprint(item)!=value['fingerprint']:continue
            item['status']='scheduled'
            tx.update(itemref,{'status':'scheduled'})
            tx.set(bot.db.collection('scheduled_posts').document(nonce+'-'+str(count)),{
                'item_id':value['id'],'route':target,'at':bot.now(),'actor':user,'status':'scheduled',
                'fingerprint':publication.fingerprint(item),'config':state['config'],'created_at':bot.now(),
                'batch_id':nonce,'previous_status':'pending'})
            count+=1
        tx.set(ref,{'value':None})
        return count
    count=await asyncio.to_thread(save,bot.db.transaction())
    await bot.say(chat,f'Сохранено заданий: {count}. Отправка начнётся при ближайшем запуске обработчика (обычно до 2 минут при свободном канале). Кнопка «Статус / отмена заданий» показывает результат; «Собрать» запускает обработку раньше.')

async def status(bot,chat,user):
    docs=[d for d in await bot.documents(bot.db.collection('scheduled_posts')) if d.to_dict().get('actor')==user and d.to_dict().get('batch_id') and d.to_dict().get('route')==bot.ROUTE.get()]
    groups={}
    for d in docs:
        j=d.to_dict();group=groups.setdefault(j['batch_id'],{'at':j['created_at'],'counts':{}})
        group['counts'][j['status']]=group['counts'].get(j['status'],0)+1
    rows=[];lines=['📣 Массовые задания текущего канала']
    labels={'scheduled':'ожидают','running':'отправляются','published':'опубликовано','failed':'ошибка','cancelled':'отменено'}
    for identity,value in sorted(groups.items(),key=lambda x:x[1]['at'],reverse=True)[:10]:
        lines.append(identity+': '+', '.join(labels.get(k,k)+' '+str(v) for k,v in value['counts'].items()))
        if value['counts'].get('scheduled'):rows.append([{'text':'Остановить '+identity,'callback_data':'massstop:'+identity}])
    failures=[d.to_dict() for d in docs if d.to_dict().get('status')=='failed']
    for failure in failures[-5:]:lines.append('⚠️ '+failure['item_id']+': '+failure.get('reason','Проверьте статус карточки'))
    if not groups:lines.append('Заданий пока нет.')
    rows.append([{'text':'Назад','callback_data':'mass'}])
    return await bot.panel(chat,'\n'.join(lines),rows)

async def cancel(bot,chat,user,identity,confirmed=False):
    if not confirmed:return await bot.say(chat,'Отменить ещё не отправленные задания? Уже опубликованные посты сохранятся.',[[{'text':'Да, остановить','callback_data':'massstopok:'+identity}],[{'text':'Назад','callback_data':'massstatus'}]])
    count=0
    for doc in await bot.documents(bot.db.collection('scheduled_posts')):
        job=doc.to_dict()
        if job.get('batch_id')!=identity or job.get('actor')!=user or job.get('route')!=bot.ROUTE.get():continue
        @bot.firestore.transactional
        def stop(tx):
            current=doc.reference.get(transaction=tx).to_dict() or {}
            itemref=bot.db.collection('items').document(current.get('item_id','missing'))
            item=itemref.get(transaction=tx).to_dict() or {}
            if current.get('status')!='scheduled':return 0
            tx.update(doc.reference,{'status':'cancelled'})
            if item.get('status')=='scheduled' and publication.fingerprint(item)==current['fingerprint']:tx.update(itemref,{'status':'pending'})
            return 1
        count+=await asyncio.to_thread(stop,bot.db.transaction())
    return await bot.say(chat,f'Отменено ожидающих заданий: {count}. Уже начатая отправка может завершиться; проверьте статус.')

async def view(bot,chat,user,index):
    state=await bot.setting(f'mass:{user}')
    if not isinstance(state,dict) or state.get('expires',bot.now())<=bot.now():return await bot.say(chat,'Просмотр истёк.')
    if index<0 or index>=len(state['items']):return
    value=state['items'][index]
    if not await bot.authorize_item(user,value['id']) or not await bot.admin_access(user,state['target']):return
    item=(await asyncio.to_thread(bot.db.collection('items').document(value['id']).get)).to_dict() or {}
    if publication.fingerprint(item)!=value['fingerprint']:return await bot.say(chat,'Материал изменился. Выберите список заново.')
    return await bot.send_item(chat,publication.draft(item,state['target']),state['config'])
