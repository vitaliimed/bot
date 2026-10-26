import unittest
from datetime import timedelta
from unittest.mock import AsyncMock,patch
import test_v2 as fixtures
from test_editor import article,CURRENT
from test_responsiveness import bot
from app import queue_recovery as recovery

class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    save=fixtures.V2Tests.save
    posts=fixtures.V2Tests.posts
    message=fixtures.V2Tests.message
    callback=fixtures.V2Tests.callback
    send=fixtures.V2Tests.send

    def setUp(self):
        fixtures.V2Tests.setUp(self)
        self.route_count=len(bot.app.routes)
        recovery.install(bot)
        self.addCleanup(self.uninstall)

    def uninstall(self):
        saved=bot._queue_recovery_originals
        for name in ('prepare_backlog','prepare_item','advanced_callback','_handle_message'):setattr(bot,name,saved[name])
        bot.jobs.run_due=saved['run_due'];bot.dispatch.endpoint=saved['dispatch_endpoint'];bot.controls.keyboard=saved['keyboard']
        bot.app.router.routes=bot.app.routes[:self.route_count]
        bot.controls.BUTTONS.pop('🩺 Восстановить очередь',None)
        for name in ('QUEUE_RECOVERY_VERSION','_queue_recovery_originals'):delattr(bot,name)

    async def cfg(self,**changes):
        await bot.put_setting('editor',{'cost_mode':'paid',**changes})
        return await bot.editor_config()

    async def test_stuck_untranslated_pending_requeues_when_writer_is_available(self):
        cfg=await self.cfg();self.save('lost',dict(article(),translation_missing=True))
        with patch.object(bot.ai_router,'available',AsyncMock(return_value={'gemini':True})):
            self.assertEqual(await recovery.recover(bot,cfg),1)
        self.assertEqual(self.db.data['items','lost']['status'],'queued');self.assertFalse(self.posts())

    async def test_no_allowed_writer_does_not_reset_or_spend(self):
        cfg=await self.cfg(cost_mode='free');self.save('lost',dict(article(),translation_missing=True))
        with patch.object(bot.ai_router,'available',AsyncMock(return_value={'openai':True})),patch.object(bot.ai_router,'execute',AsyncMock()) as api:
            self.assertEqual(await recovery.recover(bot,cfg),0)
        api.assert_not_awaited();self.assertEqual(self.db.data['items','lost']['status'],'pending')

    async def test_recovery_excludes_expired_published_and_uncertain(self):
        cfg=await self.cfg()
        self.save('old',dict(article(),created_at=CURRENT-timedelta(days=10),translation_missing=True))
        self.save('sent',dict(article(),status='published',translation_missing=True))
        self.save('uncertain',dict(article(),status='needs_reconciliation',translation_missing=True))
        with patch.object(bot.ai_router,'available',AsyncMock(return_value={'gemini':True})):
            self.assertEqual(await recovery.recover(bot,cfg,True),0)

    async def test_backoff_and_maximum_three_recoveries(self):
        cfg=await self.cfg();self.save('lost',dict(article(),translation_missing=True))
        with patch.object(bot.ai_router,'available',AsyncMock(return_value={'gemini':True})):
            for n in range(3):
                with patch.object(bot,'now',return_value=CURRENT+timedelta(minutes=16*n)):
                    self.assertEqual(await recovery.recover(bot,cfg),1)
                    self.db.collection('items').document('lost').update({'status':'pending'})
                    self.assertEqual(await recovery.recover(bot,cfg),0)
            with patch.object(bot,'now',return_value=CURRENT+timedelta(hours=1)):
                self.assertEqual(await recovery.recover(bot,cfg),0)

    async def test_native_ukrainian_gets_requested_review_without_retranslation(self):
        cfg=await self.cfg(review_provider='gemini')
        item=dict(article(),source_language='uk',summary='У Гельсінкі відкрили українську бібліотеку для родин.',source_images=False)
        self.save('ua',item)
        with patch.object(bot,'ai_prepare',AsyncMock(side_effect=AssertionError('Do not translate Ukrainian'))),patch.object(bot.ai_router,'execute',AsyncMock(return_value={'approved':True,'sensitive':False,'reason':'Відповідає джерелу'})) as reviewer:
            result=await bot.prepare_item(self.db.collection('items').document('ua'),item,cfg)
        self.assertTrue(result['review_passed']);self.assertEqual(result['text'],item['summary']);reviewer.assert_awaited_once()

    async def test_dispatch_publishes_ready_auto_news_without_scheduled_jobs(self):
        await self.cfg();await bot.put_setting('autopublish',True);self.save('ready',dict(article(),route='news'))
        with patch.object(bot.ai_router,'execute',AsyncMock(side_effect=AssertionError('No AI'))):result=await bot.dispatch.endpoint(bot)
        self.assertEqual(result['news']['published'],1);self.assertEqual(len(self.posts()),1)
        self.assertEqual(await bot.dispatch.endpoint(bot),{'news':{'published':0}})

    async def test_dispatch_respects_manual_moderation(self):
        await self.cfg(moderation=True);await bot.put_setting('autopublish',True);self.save('ready',dict(article(),route='news'))
        result=await bot.dispatch.endpoint(bot)
        self.assertEqual(result['news']['published'],0);self.assertFalse(self.posts())

    async def test_install_idempotent_and_preserves_custom_provider_config(self):
        cfg=await self.cfg();cfg['haimaker_model']='custom-model';await bot.put_setting('editor',cfg)
        current=bot.prepare_backlog;recovery.install(bot)
        self.assertIs(current,bot.prepare_backlog);self.assertEqual((await bot.setting('editor'))['haimaker_model'],'custom-model')

    async def test_recovery_never_relabels_foreign_original_as_ukrainian(self):
        cfg=await self.cfg(cost_mode='off',text_mode='auto',language='українська')
        item=dict(article(),source_language='fi',summary='Helsingissä avataan uusi kirjasto.',source_images=False)
        self.save('fi',item);result=await bot.prepare_item(self.db.collection('items').document('fi'),item,cfg)
        self.assertTrue(result['translation_missing']);self.assertEqual(result['text'],item['summary'])

    async def test_cached_translation_can_recover_failed_reviewer_without_translation_call(self):
        cfg=await self.cfg(review_provider='gemini')
        item=dict(article(),source_language='fi',summary='A new library in Helsinki',source_images=False)
        self.save('cached',item)
        draft={'title':'Нова бібліотека у Гельсінкі','body':'Відкрито бібліотеку.', 'category':'news','sensitive':False,'has_missing_event_details':False,'review_passed':False}
        with patch.object(bot,'ai_prepare',AsyncMock(return_value=draft)) as translate,patch.object(bot.ai_router,'execute',AsyncMock(return_value={'approved':True,'sensitive':False,'reason':'OK'})) as review:
            await bot.prepare_item(self.db.collection('items').document('cached'),item,cfg)
            result=await bot.prepare_item(self.db.collection('items').document('cached'),item,cfg)
        translate.assert_awaited_once();review.assert_awaited_once();self.assertTrue(result['review_passed'])

    async def test_repair_confirmation_is_single_use(self):
        with patch.object(bot,'publication_report',AsyncMock(return_value={'auto':True,'writers':['gemini'],'counts':{},'pending_reasons':{}})),patch.object(bot,'request_collection',AsyncMock()) as request:
            await recovery.show(bot,123,123)
            state=await bot.setting('repair:123')
            await bot.advanced_callback(123,123,'repairconfirm:'+state['nonce'])
            await bot.advanced_callback(123,123,'repairconfirm:'+state['nonce'])
        request.assert_awaited_once();self.assertTrue(await bot.setting('recovery_requested'))

    async def test_stuck_news_recovers_translates_and_publishes_end_to_end(self):
        cfg=await self.cfg(review_provider='off',image_mode='template')
        await bot.put_setting('autopublish',True)
        item=dict(article(),route='news',source_language='fi',summary='Helsingissä avataan uusi kirjasto.',translation_missing=True,source_images=False)
        self.save('pipeline',item)
        draft={'title':'У Гельсінкі відкрили нову бібліотеку','body':'У місті відкрили бібліотеку.', 'category':'news','sensitive':False,'has_missing_event_details':False}
        stats={'attempted':0,'published':0,'prepared':0,'errors':0,'filtered':0,'expired':0}
        with patch.object(bot.ai_router,'available',AsyncMock(return_value={'gemini':True})),patch.object(bot,'ai_prepare',AsyncMock(return_value=draft)):
            await bot.prepare_backlog(cfg,stats,bot.time.monotonic(),set())
        self.assertEqual(self.db.data['items','pipeline']['status'],'published')
        self.assertEqual(stats['published'],1);self.assertEqual(len(self.posts()),1)
        self.assertIn('У місті',self.posts()[0].args[1]['caption'])
