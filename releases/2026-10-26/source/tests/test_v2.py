import asyncio
import io
import json
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock,patch
from PIL import Image
from bs4 import BeautifulSoup
from fake_db import Database
from test_responsiveness import bot
from test_editor import CURRENT,article
from test_channels import event
from app import policy,usage,ai,templates,local_editor,publication,intake,channels,jobs,workflows,sources


class V2Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db=Database();self.sequence=0
        self.telegram=AsyncMock(side_effect=self.send)
        for p in (patch.object(bot,'db',self.db),patch.object(bot.firestore,'transactional',self.db.transactional),
                  patch.object(bot,'now',return_value=CURRENT),patch.object(bot,'tg',self.telegram),
                  patch.object(bot,'admin_access',AsyncMock(return_value=True)),patch.object(bot,'key_access',AsyncMock(return_value=True)),
                  patch.object(usage,'database',self.db),patch.object(bot.media,'fetch',AsyncMock(side_effect=AssertionError('No live network in tests')))):
            p.start();self.addCleanup(p.stop)
        bot.ROUTE.set('news');bot.app.state.gemini_client=object()
        bot.CHANNELS.clear();bot.CHANNELS.update({'news':{'telegram_id':bot.CHANNEL_ID,'type':'news','name':'Новости','enabled':True},
            'events':{'telegram_id':bot.EVENTS_CHANNEL_ID,'type':'events','name':'События','enabled':True}})
        ai.COOLDOWNS.clear()
    async def send(self,method,payload,**kwargs):
        self.sequence+=1
        if method=='getMe':return {'id':900,'username':'fixture_bot'}
        if method=='getChatMember':return {'status':'administrator','can_post_messages':True}
        return {'message_id':self.sequence,**({'photo':[{'file_id':'photo-'+str(self.sequence)}]} if method=='sendPhoto' else {})}
    def save(self,identity,item):self.db.collection('items').document(identity).set(item)
    def message(self,text,**extra):return {'message_id':3,'chat':{'id':123,'type':'private'},'from':{'id':123},'text':text,**extra}
    def callback(self,data):return {'id':'query','from':{'id':123},'data':data,'message':{'message_id':4,'chat':{'id':123,'type':'private'}}}
    def posts(self):return [c for c in self.telegram.await_args_list if c.args[0] in ('sendPhoto','sendMessage') and isinstance(c.args[1]['chat_id'],str)]

    async def test_create_from_scratch_with_zero_ai_keys(self):
        with patch.object(ai,'available',AsyncMock(side_effect=AssertionError('AI discovery forbidden'))), patch.object(bot,'ai_prepare',AsyncMock(side_effect=AssertionError('AI forbidden'))):
            await bot.handle_message(self.message('/newpost'))
            await bot.handle_message(self.message('Мій власний пост\n\nПишу текст самостійно.'))
            identity=next(i for c,i in self.db.data if c=='items')
            item=self.db.data['items',identity]
            self.assertEqual(item['title'],'Мій власний пост')
            self.assertIn('\n\n',item['text'])
            await bot.prepare_manual_send(123,123,identity,'news')
            self.assertFalse(self.posts())
            state=await bot.setting('manual_send:123')
            await bot.confirm_manual_send(123,123,state['nonce'])
            self.assertEqual(len(self.posts()),1)
            self.assertEqual(self.posts()[0].args[0],'sendPhoto')

    async def test_ukrainian_material_never_calls_ai_and_keeps_original_excerpt(self):
        item=dict(article(),status='queued',summary='У Гельсінкі відкрили українську бібліотеку для родин.',source_language='uk',source_images=False)
        self.save('ua',item)
        with patch.object(bot,'ai_prepare',AsyncMock(side_effect=AssertionError('AI forbidden'))) as request:
            result=await bot.prepare_item(self.db.collection('items').document('ua'),item,policy.config({'image_mode':'template'}))
        request.assert_not_awaited();self.assertEqual(result['text'],item['summary'])
        self.assertEqual(result['text_method'],'original');self.assertEqual(result['image_kind'],'template')

    async def test_original_mode_foreign_text_is_not_sold_as_translation(self):
        item=dict(article(),status='queued',summary='Helsingissä avataan uusi kirjasto.',source_language='fi')
        self.save('fi',item)
        with patch.object(bot,'ai_prepare',AsyncMock()) as request:
            result=await bot.prepare_item(self.db.collection('items').document('fi'),item,
                policy.config({'cost_mode':'off','text_mode':'original','image_mode':'template'}))
        request.assert_not_awaited();self.assertEqual(result['text'],item['summary']);self.assertFalse(result['translation_missing'])

    async def test_failed_free_translation_keeps_original_for_manual_review(self):
        item=dict(article(),status='queued',summary='Helsingissä uusi kirjasto.',source_language='fi')
        self.save('fi',item)
        with patch.object(bot,'ai_prepare',AsyncMock(return_value=None)):
            result=await bot.prepare_item(self.db.collection('items').document('fi'),item,policy.config({'image_mode':'template'}))
        self.assertEqual(result['status'],'pending');self.assertTrue(result['translation_missing'])
        self.assertIn('перевода',policy.automatic_reason(result,policy.config(),CURRENT,{}))

    async def test_translation_cache_avoids_second_ai_call(self):
        output={'title':'Бібліотека','body':'Нові години роботи.','category':'news','sensitive':False,'has_missing_event_details':False}
        item=dict(article(),status='queued',summary='Kirjasto Helsingissä.',source_language='fi')
        cfg=policy.config({'image_mode':'template'})
        with patch.object(bot,'ai_prepare',AsyncMock(return_value=output)) as request:
            for identity in ('one','two'):
                self.save(identity,item)
                result=await bot.prepare_item(self.db.collection('items').document(identity),item,cfg)
        request.assert_awaited_once();self.assertEqual(result['text_method'],'cache')

    async def test_free_mode_cannot_fall_back_to_paid_api(self):
        cfg=policy.config({'ai_order':['openai','gemini'],'cost_mode':'free'})
        with patch.object(ai,'available',AsyncMock(return_value={'openai':True,'gemini':True})),patch.object(ai,'request_json',AsyncMock()) as request:
            self.assertIsNone(await ai.prepare(None,'Title','Facts','https://example.org','news',cfg,bot.CATEGORIES))
        request.assert_not_awaited()
        self.assertFalse(usage.allowed('openrouter','openai/gpt-4.1-mini',cfg))
        self.assertTrue(usage.allowed('openrouter','openrouter/free',cfg))

    async def test_daily_budget_is_atomic_and_monthly_budget_survives_new_calls(self):
        cfg=policy.config({'ai_daily_limit':1,'ai_monthly_limit':1,'cost_mode':'free'})
        results=await asyncio.gather(*(usage.reserve('openrouter','openrouter/free',cfg) for _ in range(2)),return_exceptions=True)
        self.assertEqual(sum(isinstance(v,str) for v in results),1)
        self.assertEqual(sum(isinstance(v,usage.BudgetBlocked) for v in results),1)

    async def test_no_ai_mode_prevents_test_requests_too(self):
        with patch.object(ai,'request_json',AsyncMock()) as request:
            with self.assertRaises(usage.BudgetBlocked):await ai.execute(None,'openai','model','test',policy.config({'cost_mode':'off'}),'test')
        request.assert_not_awaited()

    async def test_unknown_api_cost_is_not_recorded_as_zero(self):
        identity=await usage.reserve('openrouter','openrouter/free',policy.config())
        await usage.finish(identity,'success',{'total_tokens':12})
        row=self.db.data['ai_requests',identity]
        self.assertIsNone(row['cost']);self.assertEqual(row['total_tokens'],12)

    async def test_no_image_and_disabled_ai_still_publish_photo(self):
        self.save('one',article())
        self.assertTrue(await bot.publish('one',override=True,target_route='news'))
        self.assertEqual(self.posts()[0].args[0],'sendPhoto')
        self.assertEqual(self.db.data['items','one']['image_kind'],'template')
        self.assertNotIn('image_bytes',self.db.data['items','one'])

    async def test_rejected_news_can_be_sent_to_two_channels_once_each(self):
        item=dict(article(),status='rejected');self.save('one',item)
        self.assertTrue(await bot.publish('one',override=True,target_route='news'))
        self.assertTrue(await bot.publish('one',override=True,target_route='events'))
        self.assertFalse(await bot.publish('one',override=True,target_route='news'))
        self.assertEqual(len(self.posts()),2)
        self.assertIn('#eventhsl',self.posts()[1].args[1]['caption'])
        self.assertEqual(set(self.db.data['items','one']['published_to']),{'news','events'})
        self.assertEqual(self.db.data['items','one']['image_tag'],'#eventhsl')

    async def test_stale_preview_and_lost_target_permission_prevent_publication(self):
        self.save('one',article())
        await bot.prepare_manual_send(123,123,'one','events')
        state=await bot.setting('manual_send:123')
        self.db.collection('items').document('one').update({'text':'Edited after preview'})
        await bot.confirm_manual_send(123,123,state['nonce']);self.assertFalse(self.posts())
        await bot.prepare_manual_send(123,123,'one','events');state=await bot.setting('manual_send:123')
        with patch.object(bot,'admin_access',AsyncMock(side_effect=lambda user,route=None:route!='events')):
            await bot.confirm_manual_send(123,123,state['nonce'])
        self.assertFalse(self.posts())

    async def test_config_change_after_preview_requires_new_confirmation(self):
        self.save('one',article());await bot.prepare_manual_send(123,123,'one','news')
        state=await bot.setting('manual_send:123');await bot.change_config('body_length',200)
        await bot.confirm_manual_send(123,123,state['nonce']);self.assertFalse(self.posts())

    async def test_expired_event_cannot_be_posted_as_upcoming_even_manually(self):
        self.save('one',event(event={'start':CURRENT-timedelta(days=1),'venue':'Library'}))
        self.assertFalse(await bot.publish('one',override=True,target_route='events'));self.assertFalse(self.posts())

    async def test_uncertain_delivery_requires_explicit_checked_absent_confirmation(self):
        self.save('one',article())
        with patch.object(bot,'send_item',AsyncMock(side_effect=TimeoutError())):
            self.assertFalse(await bot.publish('one',override=True,target_route='news'))
        self.assertFalse(await bot.publish('one',override=True,target_route='news'))
        await bot.reconcile_send(123,123,'one')
        self.assertEqual(self.db.data['items','one']['status'],'needs_reconciliation')
        await bot.reconcile_send(123,123,'one',True)
        self.assertTrue(await bot.publish('one',override=True,target_route='news'))

    async def test_schedule_is_durable_and_does_not_send_before_due_time(self):
        self.save('one',article());await bot.prepare_manual_send(123,123,'one','news')
        state=await bot.setting('manual_send:123');state['scheduled_at']=CURRENT+timedelta(hours=1)
        self.assertTrue(await jobs.schedule(bot,123,state))
        self.assertEqual(await jobs.run_due(bot,'news'),0);self.assertFalse(self.posts())
        with patch.object(bot,'now',return_value=CURRENT+timedelta(hours=2)):
            self.assertEqual(await jobs.run_due(bot,'news'),1)
            self.assertEqual(await jobs.run_due(bot,'news'),0)
        self.assertEqual(len(self.posts()),1)

    async def test_schedule_does_not_publish_modified_card(self):
        self.save('one',article());await bot.prepare_manual_send(123,123,'one','news')
        state=await bot.setting('manual_send:123');state['scheduled_at']=CURRENT+timedelta(minutes=10)
        await jobs.schedule(bot,123,state);self.db.collection('items').document('one').update({'text':'changed'})
        with patch.object(bot,'now',return_value=CURRENT+timedelta(hours=1)):self.assertEqual(await jobs.run_due(bot,'news'),0)
        self.assertFalse(self.posts())

    async def test_bulk_needs_confirmation_and_is_single_use(self):
        self.save('one',article());await bot.put_setting('selection:123',['one'])
        await jobs.prepare_bulk(bot,123,123,'publish','news');self.assertFalse(self.posts())
        state=await bot.setting('bulk:123');await jobs.confirm_bulk(bot,123,123,state['nonce'])
        await jobs.confirm_bulk(bot,123,123,state['nonce']);self.assertEqual(len(self.posts()),1)

    async def test_pending_queue_beyond_sixty_blocked_items_is_not_starved(self):
        self.db.collection('sources').document('off').set({'enabled':False})
        self.save('ready',article())
        for n in range(70):self.save(str(n),dict(article(),url=f'https://example.org/{n}',sensitive=True))
        await bot.put_setting('autopublish',True)
        stats=await bot.collect_batch();self.assertEqual(stats['published'],1)
        self.assertEqual(self.db.data['items','ready']['status'],'published')

    async def test_processing_backlog_runs_before_network_source_fetch(self):
        self.save('queued',dict(article(),status='queued',source_language='uk',summary='Український текст.',source_images=False))
        self.db.collection('sources').document('feed').set({'enabled':True,'url':'https://example.org/rss','category':'news'})
        events=[]
        async def prepare(ref,item,cfg):events.append('prepare');return dict(item,status='pending')
        async def fetch(*args):events.append('fetch');return [],{}
        with patch.object(bot,'prepare_item',AsyncMock(side_effect=prepare)),patch.object(sources,'fetch',AsyncMock(side_effect=fetch)):
            await bot.collect_batch()
        self.assertLess(events.index('prepare'),events.index('fetch'))

    async def test_old_news_is_recorded_instead_of_dropped(self):
        self.db.collection('sources').document('feed').set({'enabled':True,'url':'https://example.org/rss','category':'news'})
        row={'title':'Very old news','summary':'Facts','url':'https://example.org/old','source_published_at':CURRENT-timedelta(days=9)}
        with patch.object(sources,'fetch',AsyncMock(return_value=([row],{}))):await bot.collect_batch()
        values=[v for (c,_),v in self.db.data.items() if c=='items']
        self.assertEqual(len(values),1);self.assertEqual(values[0]['status'],'expired')

    async def test_identical_story_across_sources_is_preserved_as_duplicate(self):
        for identity in ('a','b'):self.db.collection('sources').document(identity).set({'enabled':True,'url':'https://example.org/'+identity,'category':'news'})
        async def fetch(source,*args):return [{'title':'A sufficiently long identical title about a new library in Helsinki','summary':'same facts','url':source['url'],'source_published_at':CURRENT-timedelta(days=8)}],{}
        with patch.object(sources,'fetch',AsyncMock(side_effect=fetch)):await bot.collect_batch()
        values=[v for (c,_),v in self.db.data.items() if c=='items']
        self.assertEqual(len(values),2);self.assertEqual(sum(v['status']=='duplicate' for v in values),1)

    async def test_forwarded_post_keeps_origin_link_and_photo(self):
        message=self.message('Перевірений український анонс',forward_origin={'type':'channel','chat':{'username':'organizer','title':'Organizer'},'message_id':55},photo=[{'file_id':'existing-photo'}])
        await bot.handle_message(message)
        item=next(v for (c,_),v in self.db.data.items() if c=='items')
        self.assertEqual(item['url'],'https://t.me/organizer/55');self.assertEqual(item['image_file_id'],'existing-photo')
        self.assertTrue(item['sensitive']);self.assertFalse(self.posts())

    async def test_facebook_url_without_facts_cannot_be_published(self):
        await bot.handle_message(self.message('/import https://www.facebook.com/events/123'))
        identity=next(i for c,i in self.db.data if c=='items')
        await bot.prepare_manual_send(123,123,identity,'news')
        self.assertIsNone(await bot.setting('manual_send:123'));self.assertFalse(self.posts())

    async def test_new_channel_uses_independent_settings_and_can_be_archived(self):
        await channels.add(bot,123,123,'@fixture_events | events | Тестовый канал')
        route=next(r for r in bot.CHANNELS if r.startswith('ch'))
        await bot.change_config('daily_limit',4)
        self.assertEqual((await bot.editor_config(route))['daily_limit'],4)
        self.assertEqual((await bot.editor_config('news'))['daily_limit'],12)
        self.assertTrue(any(v.get('route')==route for (c,_),v in self.db.data.items() if c=='sources'))
        await bot.advanced_callback(123,123,'chremove:'+route)
        self.assertTrue(bot.CHANNELS[route]['enabled'])
        await bot.advanced_callback(123,123,'chremoveok:'+route)
        self.assertFalse(bot.CHANNELS[route]['enabled'])

    async def test_setting_dialog_and_cancel_do_not_consume_later_text(self):
        await bot.handle_callback(self.callback('ctx:news|inputsetting:signature'))
        await bot.handle_message(self.message('Нова підпис'))
        self.assertEqual((await bot.editor_config())['signature'],'Нова підпис')
        await bot.handle_callback(self.callback('ctx:news|inputsetting:signature'))
        await bot.handle_message(self.message('/cancel'))
        await bot.handle_message(self.message('Не настройка'))
        self.assertEqual((await bot.editor_config())['signature'],'Нова підпис')

    async def test_backup_restore_preserves_keys_and_items(self):
        self.save('one',article());await bot.change_config('daily_limit',7)
        self.db.collection('ai_credentials').document('openai').set({'mode':'disabled'})
        await workflows.backup(bot,123,123)
        identity=next(i for c,i in self.db.data if c=='config_backups')
        await bot.change_config('daily_limit',2)
        await workflows.restore_backup(bot,123,123,identity,True)
        self.assertEqual((await bot.editor_config())['daily_limit'],7)
        self.assertIn(('items','one'),self.db.data);self.assertEqual(self.db.data['ai_credentials','openai']['mode'],'disabled')

    async def test_image_failure_falls_back_to_local_template_without_paid_request(self):
        with patch.object(bot.media,'source_image',AsyncMock(side_effect=TimeoutError())),patch.object(bot.media,'generate_openai',AsyncMock()) as paid:
            image=await bot.prepare_image(article(),policy.config({'cost_mode':'free','image_mode':'source_generate'}))
        paid.assert_not_awaited();self.assertEqual(image['image_kind'],'template')

    async def test_source_copy_keeps_original_and_is_idempotent(self):
        from app import operations
        self.db.collection('sources').document('feed').set({'route':'news','url':'https://example.org/feed','kind':'rss','enabled':True})
        await operations.assign(bot,123,123,'feed','events')
        await operations.assign(bot,123,123,'feed','events')
        rows=[v for (c,_),v in self.db.data.items() if c=='sources']
        self.assertEqual(len(rows),2)
        self.assertEqual(self.db.data['sources','feed']['route'],'news')

    async def test_archive_operation_needs_confirmation_and_preserves_material(self):
        from app import operations
        self.save('one',article())
        await operations.prepare(bot,123,123,'archive')
        self.assertEqual(self.db.data['items','one']['status'],'pending')
        state=await bot.setting('bulk:123')
        await jobs.confirm_bulk(bot,123,123,state['nonce'])
        self.assertEqual(self.db.data['items','one']['status'],'rejected')

    async def test_watchdog_does_not_publish_or_use_ai(self):
        from app import operations
        self.save('one',article())
        with patch.object(ai,'request_json',AsyncMock()) as request:
            report=await operations.watchdog(bot)
        request.assert_not_awaited();self.assertFalse(self.posts())
        self.assertIn('news',report)

    async def test_dry_run_blocks_real_publication(self):
        import os
        self.save('one',article())
        with patch.dict(os.environ,{'DRY_RUN':'1'}):self.assertFalse(await bot.publish('one',override=True))
        self.assertFalse(self.posts());self.assertEqual(self.db.data['items','one']['status'],'pending')


    async def test_off_mode_does_not_require_legacy_ai_reviewer(self):
        cfg=policy.config({'cost_mode':'off','review_provider':'gemini','interval_minutes':0,'window_start':'00:00','window_end':'23:59'})
        item=dict(article(),sensitive=False,missing_details=False,translation_missing=False)
        self.assertNotEqual(policy.automatic_reason(item,cfg,CURRENT,{}),'Нет одобрения проверяющего ИИ')

    async def test_mass_hundred_queued_with_no_ai_or_channel_calls(self):
        from app import mass
        with patch.object(bot,'ai_prepare',AsyncMock(side_effect=AssertionError('No AI'))):
            state=await self.mass_fixture(100)
            self.assertEqual(len(state['items']),100)
            await mass.confirm(bot,123,123,state['nonce'])
        self.assertEqual(len(list(self.db.collection('scheduled_posts').stream())),100)
        self.assertFalse(self.posts())

    async def test_ai_test_blocked_mode_does_not_call_api_or_set_cooldown(self):
        with patch.object(ai,'execute',AsyncMock()) as request:
            await bot.handle_callback(self.callback('aitest:openai'))
        request.assert_not_awaited()
        self.assertFalse(ai.COOLDOWNS)
        text=' '.join(str(c.args) for c in self.telegram.await_args_list)
        self.assertIn('Бесплатный режим',text)
        self.assertNotIn('таймаут или недоступность',text)

    def test_cloudflare_missing_account_diagnostic_without_request(self):
        from app import ai_diagnostics
        cfg=policy.config({'cloudflare_free_account':True})
        self.assertIn('Account ID',ai_diagnostics.preflight('cloudflare',cfg))
        self.assertIn('лимитом',ai_diagnostics.explain(usage.BudgetBlocked('secret should not escape')))
        self.assertNotIn('secret',ai_diagnostics.explain(ValueError('secret')))

    async def test_story_lock_blocks_different_urls_same_long_title(self):
        title='У Гельсінкі відкрили нову українську бібліотеку для всіх родин'
        for identity in ('one','two'):self.save(identity,dict(article(),title=title,url='https://example.org/'+identity))
        values=await asyncio.gather(bot.publish('one',override=True),bot.publish('two',override=True))
        self.assertEqual(sum(values),1)
        self.assertEqual(len(self.posts()),1)

    async def test_story_lock_separates_channels_and_event_dates(self):
        from app import uniqueness
        title='Велика виставка української культури у міській бібліотеці'
        first=dict(article(),title=title)
        second=dict(first,event={'start':CURRENT+timedelta(days=1)})
        self.assertNotEqual(uniqueness.keys(first,'news'),uniqueness.keys(first,'events'))
        self.assertNotEqual(uniqueness.keys(first,'events'),uniqueness.keys(second,'events'))
        self.assertEqual(uniqueness.keys(article(),'news'),[])

    async def test_story_uncertain_send_stays_blocked_until_reconciliation(self):
        title='Нові правила руху автомобілів у центрі Гельсінкі починають діяти'
        for identity in ('one','two'):self.save(identity,dict(article(),title=title,url='https://example.org/'+identity))
        with patch.object(bot,'send_item',AsyncMock(side_effect=TimeoutError())):
            self.assertFalse(await bot.publish('one',override=True))
        self.assertFalse(await bot.publish('two',override=True))
        await bot.reconcile_send(123,123,'one')
        await bot.reconcile_send(123,123,'one',True)
        self.assertTrue(await bot.publish('two',override=True))

    async def test_dispatch_only_confirmed_jobs_and_no_ai(self):
        from app import mass,dispatch
        state=await self.mass_fixture(4)
        await mass.confirm(bot,123,123,state['nonce'])
        with patch.object(bot,'prepare_item',AsyncMock(side_effect=AssertionError('No preparation'))),patch.object(bot,'collect_batch',AsyncMock(side_effect=AssertionError('No collection'))):
            self.assertEqual((await dispatch.endpoint(bot))['news']['published'],3)
            self.assertEqual((await dispatch.endpoint(bot))['news']['published'],1)
        self.assertEqual(await dispatch.endpoint(bot),{})
        self.assertEqual(len(self.posts()),4)

    async def test_dispatch_skips_active_collector_lease(self):
        from app import dispatch
        self.db.collection('settings').document(bot.route_key('collector_lease','news')).set({'until':CURRENT+timedelta(minutes=1),'token':'collector'})
        self.assertEqual(await dispatch.run(bot,'news'),{'busy':True})
        self.assertFalse(self.posts())

    async def test_fastnews_needs_confirmation_and_preserves_filters(self):
        from app import operations
        await bot.put_setting('editor',{'exclude_words':'спорт','categories':['news']})
        await operations.fastnews(bot,123,123)
        self.assertFalse(await bot.setting('autopublish',False))
        await operations.fastnews(bot,123,123,True)
        cfg=await bot.editor_config()
        self.assertEqual(cfg['cost_mode'],'off');self.assertEqual(cfg['text_mode'],'original')
        self.assertEqual(cfg['exclude_words'],'спорт');self.assertEqual(cfg['categories'],['news'])
        self.assertTrue(await bot.setting('autopublish'))
        self.assertFalse(self.posts())

    def test_priority_sort_does_not_override_filters(self):
        cfg=policy.config({'priority_words':'транспорт,робота','exclude_words':'спорт'})
        important=dict(article(),title='Робота і транспорт у Гельсінкі')
        self.assertGreater(policy.priority(important,cfg),policy.priority(article(),cfg))
        self.assertFalse(policy.topic_allowed(dict(important,text='спорт'),cfg))

    def test_custom_templates_are_plain_text_and_cannot_evaluate_attributes(self):
        cfg=policy.config({'title_template':'📰 {title}','body_template':'Коротко: {body}'})
        rendered=bot.format_post(dict(article(),title='<script>bad</script>'),cfg,True)
        self.assertIn('📰',rendered);self.assertIn('Коротко:',rendered)
        self.assertNotIn('<script>',rendered)
        for pattern in ('{title.__class__}','{title!r}','{title:10000000}','Missing placeholder'):
            with self.assertRaises(ValueError):policy.validate('title_template',pattern)

    async def test_expired_event_cannot_be_sent_to_news_as_upcoming(self):
        self.save('past',event(event={'start':CURRENT-timedelta(days=1),'venue':'Helsinki'}))
        self.assertFalse(await bot.publish('past',target_route='news',override=True))
        self.assertFalse(self.posts())

    async def test_migration_backfills_story_index_without_changing_history(self):
        from app import uniqueness
        from types import SimpleNamespace
        title='У Гельсінкі відкрили український освітній центр для дітей'
        self.save('published',dict(article(),title=title,route='news',status='published',published_at=CURRENT,
            published_to={'news':{'message_id':1234,'channel_id':bot.CHANNEL_ID,'published_at':CURRENT}}))
        request=SimpleNamespace(headers={'X-Cron-Secret':bot.CRON_SECRET})
        with patch.object(bot,'seed_catalog',AsyncMock(return_value=0)),patch.object(bot,'CATALOG',{}):
            result=await bot.migrate_endpoint(request)
            repeated=await bot.migrate_endpoint(request)
        self.assertGreater(result['story_locks_added'],0);self.assertEqual(repeated['story_locks_added'],0)
        self.assertEqual(self.db.data['items','published']['published_to']['news']['message_id'],1234)
        self.save('copy',dict(article(),title=title,url='https://different.example/news'))
        self.assertFalse(await bot.publish('copy',override=True))
        self.assertFalse(self.posts())

    async def mass_fixture(self,count=5):
        from app import mass
        for n in range(count):self.save('mass-'+str(n),dict(article(),url='https://example.org/mass/'+str(n),title='Заголовок '+str(n),status='pending',sensitive=False))
        await mass.pick(bot,123,123,100)
        await mass.preview(bot,123,123,'news')
        return await bot.setting('mass:123')

    async def test_mass_confirmation_durable_single_use_and_preview_no_mutation(self):
        from app import mass
        state=await self.mass_fixture()
        await mass.view(bot,123,123,0)
        self.assertFalse(self.posts())
        await asyncio.gather(mass.confirm(bot,123,123,state['nonce']),mass.confirm(bot,123,123,state['nonce']))
        jobs_saved=list(self.db.collection('scheduled_posts').stream())
        self.assertEqual(len(jobs_saved),5)
        self.assertFalse(self.posts())
        self.assertEqual(await jobs.run_due(bot,'news'),3)
        self.assertEqual(await jobs.run_due(bot,'news'),2)
        self.assertEqual(await jobs.run_due(bot,'news'),0)
        self.assertEqual(len(self.posts()),5)

    async def test_mass_cancel_restores_pending_without_publication(self):
        from app import mass
        state=await self.mass_fixture()
        await mass.confirm(bot,123,123,state['nonce'])
        await mass.cancel(bot,123,123,state['nonce'],True)
        self.assertEqual(await jobs.run_due(bot,'news'),0)
        self.assertTrue(all(d.to_dict()['status']=='pending' for d in self.db.collection('items').stream()))
        self.assertFalse(self.posts())

    async def test_mass_rejects_changed_config_and_stale_cards(self):
        from app import mass
        state=await self.mass_fixture(2)
        self.db.collection('items').document('mass-0').update({'title':'Редактор изменил заголовок'})
        await mass.confirm(bot,123,123,state['nonce'])
        self.assertEqual(len(list(self.db.collection('scheduled_posts').stream())),1)
        await bot.put_setting('editor',{'silent':True})
        self.assertEqual(await jobs.run_due(bot,'news'),0)
        self.assertFalse(self.posts())

    async def test_mass_no_permissions_and_expired_confirmation(self):
        from app import mass
        state=await self.mass_fixture(1)
        with patch.object(bot,'admin_access',AsyncMock(return_value=False)):
            await mass.confirm(bot,123,123,state['nonce'])
        self.assertFalse(list(self.db.collection('scheduled_posts').stream()))
        with patch.object(bot,'now',return_value=CURRENT+timedelta(minutes=20)):
            await mass.confirm(bot,123,123,state['nonce'])
        self.assertFalse(list(self.db.collection('scheduled_posts').stream()))

    async def test_mass_excludes_expired_events(self):
        from app import mass
        self.save('old',event(status='pending',event={'start':CURRENT-timedelta(days=1),'venue':'Helsinki'}))
        bot.ROUTE.set('events')
        await mass.pick(bot,123,123,10)
        await mass.preview(bot,123,123,'events')
        self.assertIsNone(await bot.setting('mass:123'))


class ParserAndTemplateTests(unittest.TestCase):
    def test_template_contains_valid_small_jpeg_for_each_style_and_long_title(self):
        for category in ('news','sport','events'):
            result=templates.create({'title':'Український заголовок '*30,'category':category},'#channel')
            im=Image.open(io.BytesIO(result['image_bytes']))
            self.assertEqual(im.size,(1200,675));self.assertLess(len(result['image_bytes']),450000)

    def test_date_only_event_is_valid_for_the_whole_local_day(self):
        data=event(event={'start':CURRENT.replace(hour=0),'date_only':True,'venue':'Hall'})
        self.assertFalse(policy.event_expired(data,CURRENT))
        self.assertTrue(policy.event_expired(data,CURRENT+timedelta(days=1)))

    def test_json_ld_preserves_known_details_and_does_not_invent_missing_fields(self):
        raw={'@type':'Event','name':'Meetup','startDate':'2026-11-01','location':{'name':'Library','address':{'addressLocality':'Helsinki'}},'offers':{'price':'0','priceCurrency':'EUR','url':'https://tickets.example/event'}}
        result=intake.page_item(('<script type="application/ld+json">'+json.dumps(raw)+'</script>').encode(),'https://example.org/event')
        self.assertTrue(result['event']['date_only']);self.assertEqual(result['event']['city'],'Helsinki')
        self.assertEqual(result['event']['address'],'');self.assertTrue(result['manual_review'])

    def test_tiketti_cancelled_events_are_not_returned_and_doors_are_not_start_time(self):
        data={'keys':['eventID','name','start_date','locationID','flags','url','timeinfo'],
            'locations':{'1':{'city':'Helsinki','name':'Hall'}},'events':[[1,'Show','2026-10-22',1,0,'show/1','Ovet klo 18:00'],[2,'Cancelled','2026-10-22',1,4,'show/2','']]}
        rows,_=intake.tiketti(json.dumps(data),{},policy.config(route='events'),CURRENT)
        self.assertEqual(len(rows),1);self.assertTrue(rows[0]['event']['date_only']);self.assertEqual(rows[0]['event']['start'].hour,0)
        self.assertEqual(rows[0]['event']['time_note'],'Ovet klo 18:00')

    def test_pro_calendar_keeps_verified_date_city_and_original_link(self):
        text='<li>To 22.10.2026, <strong>Українська зустріч</strong>, <a href="https://venue.example">Library</a>, Helsinki, klo 18:30 <a href="https://event.example">Sivusto</a><img src="/FREE.png"></li>'
        rows=intake.pro_ukraina(text,{'url':'https://www.proukraina.fi/calendar'},policy.config(route='events'),CURRENT)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['event']['start'].hour,18)
        self.assertEqual(rows[0]['url'],'https://event.example');self.assertTrue(rows[0]['event']['is_free'])

    def test_very_full_event_caption_stays_within_telegram_limit(self):
        item=event(image_kind='source',image_credit='Автор '*30,event={'start':CURRENT,'venue':'Місце '*50,'city':'Helsinki',
            'price':'Ціна '*50,'contact':'Контакти '*50,'time_note':'Час '*50,'age_limit':'Вік '*40,'online':True,'sold_out':True,'ticket_url':'https://example.org/ticket'})
        cfg=policy.config({'signature':'Підпис '*30},route='events')
        value=bot.format_post(item,cfg,True)
        self.assertLessEqual(len(BeautifulSoup(value,'html.parser').get_text().encode('utf-16-le'))//2,1024)
        self.assertIn('#eventhsl',value);self.assertIn('📷',value);self.assertIn('https://example.org/news',value)




if __name__=='__main__':unittest.main()
