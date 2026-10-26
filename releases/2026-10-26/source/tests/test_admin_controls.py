import json
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from app import credentials, controls, policy, ai
from fake_db import Database
from test_responsiveness import bot
from test_editor import article, CURRENT
from test_channels import event


class KeyStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database()
        self.secret = MagicMock()
        self.secret.add_secret_version.return_value = SimpleNamespace(name='projects/12345/secrets/bot-ai-openai/versions/7')
        self.secret.access_secret_version.return_value = SimpleNamespace(payload=SimpleNamespace(data=b'sk-fixture-secret-123456789012345'))
        self.store = credentials.KeyStore(self.db,'fixture-project',self.secret)

    async def test_key_payload_only_goes_to_secret_manager_and_numeric_project_is_normalized(self):
        value = 'sk-fixture-secret-123456789012345'
        await self.store.save('openai',value,123)
        self.assertNotIn(value,str(self.db.data))
        stored = self.db.data['ai_credentials','openai']
        self.assertEqual(stored['version'],'projects/fixture-project/secrets/bot-ai-openai/versions/7')
        self.assertEqual(await self.store.key('openai'),value)
        self.assertEqual(self.secret.add_secret_version.call_args.kwargs['request']['payload']['data'],value.encode())

    async def test_removal_invalidates_other_instances_cache_and_old_environment_key(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'old-env-key'}):
            self.assertEqual(await self.store.key('openai'),'old-env-key')
            await self.store.save('openai','sk-fixture-secret-123456789012345',123)
            other = credentials.KeyStore(self.db,'fixture-project',self.secret)
            self.assertTrue(await other.key('openai'))
            await self.store.remove('openai',123)
            self.assertEqual(await other.key('openai'),'')
            self.assertFalse((await other.describe('openai'))['configured'])
            fresh = credentials.KeyStore(self.db,'fixture-project',self.secret)
            self.assertEqual(await fresh.key('openai'),'')

    async def test_forged_secret_reference_cannot_read_unrelated_secret(self):
        for version in ('projects/elsewhere/secrets/bot-ai-openai/versions/7',
                        'projects/fixture-project/secrets/bot-token/versions/1',
                        'projects/fixture-project/secrets/bot-ai-openai/versions/latest'):
            self.db.collection('ai_credentials').document('openai').set({'mode':'secret','version':version})
            self.assertEqual(await self.store.key('openai'),'')
        self.secret.access_secret_version.assert_not_called()

    async def test_failed_secret_read_does_not_reactivate_environment_key(self):
        await self.store.save('openai','sk-fixture-secret-123456789012345',123)
        self.secret.access_secret_version.side_effect = RuntimeError('denied')
        with patch.dict(os.environ,{'OPENAI_API_KEY':'old-env-key'}):
            with self.assertRaises(RuntimeError): await self.store.key('openai')

    async def test_text_api_reads_runtime_key_and_removal_stops_network_request(self):
        value = 'sk-fixture-secret-123456789012345'
        await self.store.save('openai',value,123)
        calls = []
        def respond(request):
            calls.append(request)
            self.assertEqual(request.headers['Authorization'],'Bearer '+value)
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'}}]})
        with patch.object(credentials,'store',self.store),patch.dict(os.environ,{'OPENAI_API_KEY':'old-env-key'}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                self.assertTrue((await ai.request_json(client,'openai','fixture-model','JSON'))['ok'])
                await self.store.remove('openai',123)
                with self.assertRaises(ValueError): await ai.request_json(client,'openai','fixture-model','JSON')
        self.assertEqual(len(calls),1)


class AdminControlsTests(unittest.IsolatedAsyncioTestCase):
    user = 123
    def setUp(self):
        self.db = Database()
        self.telegram = AsyncMock(side_effect=self.telegram_result)
        self.vault = AsyncMock()
        self.vault.describe.return_value = {'mode':'missing','configured':False}
        self.vault.key.return_value = ''
        patches = [patch.object(bot,'db',self.db),patch.object(bot.firestore,'transactional',self.db.transactional),
                   patch.object(bot,'tg',self.telegram),patch.object(bot,'admin_access',AsyncMock(return_value=True)),
                   patch.object(credentials,'store',self.vault),patch.dict(os.environ,{'AI_KEY_MANAGEMENT':'1'})]
        for p in patches: p.start(); self.addCleanup(p.stop)
        bot.ROUTE.set('news')
        ai.COOLDOWNS.clear(); ai.KEY_STATES.clear()

    async def telegram_result(self,method,payload,**kwargs):
        if method == 'getChatMember': return {'status':'creator','user':{'id':self.user,'is_bot':False}}
        return {'message_id':42}

    def message(self,text):
        return {'message_id':21,'chat':{'id':self.user,'type':'private'},'from':{'id':self.user},'text':text}
    def callback(self,data):
        return {'id':'fixture','from':{'id':self.user},'data':data,'message':{'message_id':42,'chat':{'id':self.user,'type':'private'}}}
    def texts(self):
        return '\n'.join(str(c.args[1].get('text','')) for c in self.telegram.await_args_list)
    def payloads(self,method):
        return [c.args[1] for c in self.telegram.await_args_list if c.args[0]==method]

    async def test_start_restores_persistent_channel_keyboard(self):
        await bot.handle_message(self.message('/start'))
        markup = self.payloads('sendMessage')[0]['reply_markup']
        self.assertTrue(markup['is_persistent']); self.assertFalse(markup['one_time_keyboard'])
        self.assertIn('✅ 📰 Новости',str(markup))
        await bot.handle_message(self.message('📅 События'))
        markup = [p['reply_markup'] for p in self.payloads('sendMessage') if p.get('reply_markup',{}).get('is_persistent')][-1]
        self.assertIn('✅ 📅 События',str(markup))
        self.assertIn('▶️ Включить авто · События',str(markup))
        self.assertEqual(await bot.selected_route(self.user),'events')

    async def test_old_channel_button_targets_its_original_channel_and_updates_panel(self):
        await bot.put_setting('route:123','events')
        await bot.handle_callback(self.callback('ctx:news|number:daily_limit:9'))
        self.assertEqual((await bot.editor_config('news'))['daily_limit'],9)
        self.assertEqual((await bot.editor_config('events'))['daily_limit'],20)
        edited = self.payloads('editMessageText')[-1]
        self.assertIn('daily_limit = 9',edited['text'])
        for row in edited['reply_markup']['inline_keyboard']:
            for button in row: self.assertTrue(button['callback_data'].startswith('ctx:news|'))

    async def test_repeated_setting_and_auto_buttons_are_idempotent(self):
        for _ in range(2):
            await bot.handle_callback(self.callback('ctx:events|auto:1'))
            await bot.handle_callback(self.callback('ctx:events|category:sport:1'))
        self.assertTrue(await bot.setting('autopublish_events'))
        self.assertFalse(await bot.setting('autopublish',False))
        self.assertEqual((await bot.editor_config('events'))['categories'].count('sport'),1)
        self.assertIn('⏸ Выключить авто · События',str(self.payloads('sendMessage')))

    async def test_legacy_ambiguous_settings_button_opens_new_menu_without_changing_state(self):
        await bot.put_setting('autopublish',True)
        await bot.handle_callback(self.callback('toggle'))
        self.assertTrue(await bot.setting('autopublish'))
        self.assertIn('старой версии',self.texts())

    async def test_expired_callback_ack_does_not_drop_a_valid_action(self):
        response = httpx.Response(400,json={'description':'Bad Request: query is too old'},request=httpx.Request('POST','https://example.org'))
        async def result(method,payload,**kwargs):
            if method == 'answerCallbackQuery': raise httpx.HTTPStatusError('expired',request=response.request,response=response)
            return {'message_id':42}
        self.telegram.side_effect = result
        await bot.handle_callback(self.callback('ctx:news|number:daily_limit:7'))
        self.assertEqual((await bot.editor_config('news'))['daily_limit'],7)

    async def test_route_bound_button_cannot_bypass_event_channel_permission(self):
        async def allowed(user,route=None): return route!='events'
        with patch.object(bot,'admin_access',AsyncMock(side_effect=allowed)):
            await bot.handle_callback(self.callback('ctx:events|number:daily_limit:99'))
        self.assertEqual((await bot.editor_config('events'))['daily_limit'],20)

    async def test_ai_disable_is_channel_specific_and_never_deletes_key(self):
        await bot.handle_callback(self.callback('ctx:events|aienable:openai:0'))
        self.assertIn('openai',(await bot.editor_config('events'))['disabled_ai'])
        self.assertNotIn('openai',(await bot.editor_config('news'))['disabled_ai'])
        self.vault.remove.assert_not_awaited()

    async def test_owner_key_entry_is_deleted_and_not_stored_in_firestore_or_responses(self):
        await bot.handle_callback(self.callback('ctx:events|keyadd:openai'))
        secret = 'sk-fixture-secret-123456789012345'
        await bot.handle_message(self.message(secret))
        self.vault.save.assert_awaited_once_with('openai',secret,self.user)
        self.assertTrue(self.payloads('deleteMessage'))
        self.assertNotIn(secret,str(self.db.data)); self.assertNotIn(secret,self.texts())
        self.assertEqual(await bot.selected_route(self.user),'events')
        self.assertIsNone(await bot.setting('input:123'))

    async def test_ordinary_admin_cannot_start_or_complete_key_change(self):
        with patch.object(bot,'key_access',AsyncMock(return_value=False)):
            await bot.handle_callback(self.callback('ctx:news|keyadd:openai'))
            self.assertIsNone(await bot.setting('input:123'))
            await bot.put_setting('input:123',{'kind':'key','provider':'openai','route':'news','nonce':'x','expires':bot.now()+timedelta(minutes=2)})
            await bot.handle_message(self.message('sk-fixture-secret-123456789012345'))
        self.vault.save.assert_not_awaited()
        self.assertTrue(self.payloads('deleteMessage'))

    async def test_expired_key_entry_and_key_pasted_into_model_are_not_reused_as_copy(self):
        secret = 'sk-fixture-secret-123456789012345'
        for kind,expires in [('key',bot.now()-timedelta(seconds=1)),('model',bot.now()+timedelta(minutes=2))]:
            await bot.put_setting('input:123',{'kind':kind,'provider':'openai','route':'news','nonce':kind,'expires':expires})
            await bot.handle_message(self.message(secret))
        self.vault.save.assert_not_awaited()
        self.assertNotIn(secret,str(self.db.data)); self.assertNotIn(secret,self.texts())

    async def test_unprefixed_key_is_not_saved_as_model(self):
        value = 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6'
        await bot.put_setting('input:123',{'kind':'model','provider':'mistral','route':'news','nonce':'x','expires':bot.now()+timedelta(minutes=2)})
        await bot.handle_message(self.message(value))
        self.assertNotIn(value,str(self.db.data)); self.assertNotIn(value,self.texts())
        self.assertTrue(self.payloads('deleteMessage'))

    async def test_menu_button_cancels_edit_without_publishing_its_label(self):
        self.db.collection('items').document('one').set(article())
        await bot.put_setting('edit_state:123','one')
        before = self.db.data['items','one']['text']
        await bot.handle_message(self.message('📊 Статистика · Новости'))
        self.assertEqual(self.db.data['items','one']['text'],before)

    async def test_accidental_key_in_ask_is_not_sent_to_ai(self):
        with patch.object(bot,'ask_assistant',AsyncMock()) as ask:
            await bot.handle_message(self.message('/ask sk-fixture-secret-123456789012345'))
        ask.assert_not_awaited(); self.assertTrue(self.payloads('deleteMessage'))

    async def test_remove_key_requires_owner_and_fresh_single_use_confirmation(self):
        await bot.handle_callback(self.callback('ctx:news|keyremove:openai'))
        self.vault.remove.assert_not_awaited()
        state = await bot.setting('input:123')
        data = 'ctx:news|keyconfirm:openai:'+state['nonce']
        await bot.handle_callback(self.callback(data))
        await bot.handle_callback(self.callback(data))
        self.vault.remove.assert_awaited_once_with('openai',123)

    async def test_status_shows_image_reason_and_publish_button_opens_destinations(self):
        self.db.collection('items').document('one').set(article())
        await bot.change_config('require_image',True)
        await bot.handle_callback(self.callback('status:one'))
        self.assertIn('бесплатная карточка',self.texts())
        await bot.handle_callback(self.callback('pub:one'))
        self.assertIn('Куда опубликовать',self.texts())
        self.assertEqual(self.db.data['items','one']['status'],'pending')
        self.assertFalse(self.payloads('sendPhoto'))
        self.assertNotIn('Не опубликовано; проверь статус',self.texts())

    async def test_source_failure_is_visible_and_never_sends_post(self):
        self.db.collection('items').document('evt').set(event())
        with patch.object(bot,'now',return_value=CURRENT),patch.object(bot.sources,'refresh_event',AsyncMock(side_effect=TimeoutError())):
            self.assertFalse(await bot.publish('evt'))
            await bot.handle_callback(self.callback('status:evt'))
        self.assertIn('Источник события не ответил',self.texts())
        self.assertEqual(self.db.data['items','evt']['status'],'pending')

    async def test_every_generated_callback_fits_telegram_limit(self):
        for section in (None,*bot.SECTIONS,'ai_options'):
            await bot.handle_callback(self.callback('ctx:events|section:'+str(section)))
        for provider in policy.PROVIDERS:
            await bot.handle_callback(self.callback('ctx:events|ai:'+provider))
        for call in self.telegram.await_args_list:
            for row in call.args[1].get('reply_markup',{}).get('inline_keyboard',[]):
                for button in row: self.assertLessEqual(len(button['callback_data'].encode()),64)


class DisabledProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.enterContext(patch.dict(policy.DEFAULTS,{'cost_mode':'paid'}))
    async def test_disabled_writer_is_skipped_and_disabled_reviewer_fails_closed(self):
        cfg = policy.config({'ai_order':['openai','gemini'],'disabled_ai':['openai'],'review_provider':'openai'})
        valid = {'title':'News','body':'Facts','category':'news','sensitive':False,'has_missing_event_details':False}
        with patch.object(ai,'available',AsyncMock(return_value={'openai':True,'gemini':True})),patch.object(ai,'request_json',AsyncMock(return_value=valid)) as request:
            result = await ai.prepare(None,'Title','Facts','https://example.org','news',cfg,bot.CATEGORIES)
        self.assertEqual(request.await_count,1)
        self.assertEqual(request.call_args.args[1],'gemini')
        self.assertFalse(result['review_passed'])

    async def test_disabled_image_provider_does_not_consume_a_generation_attempt(self):
        cfg = policy.config({'image_mode':'generate','image_order':['openai'],'disabled_ai':['openai']})
        with patch.object(ai,'available',AsyncMock(return_value={'openai':True})),patch.object(bot,'reserve_image',AsyncMock()) as reserve:
            result = await bot.prepare_image(article(),cfg)
        reserve.assert_not_awaited(); self.assertEqual(result['image_kind'],'template')

    async def test_images_use_runtime_secret_even_without_environment_key(self):
        bot.app.state.gemini_client = object()
        cfg = policy.config({'image_mode':'generate','image_order':['openai']})
        value = 'sk-runtime-fixture-123456789012345'
        with patch.object(ai,'available',AsyncMock(return_value={'openai':True})), \
             patch.object(credentials,'get_key',AsyncMock(return_value=value)), \
             patch.object(bot,'reserve_image',AsyncMock(return_value=True)), \
             patch.object(bot.media,'generate_openai',AsyncMock(return_value={'image_kind':'generated'})) as image:
            result = await bot.prepare_image(article(),cfg)
        self.assertEqual(image.call_args.args[3],value)
        self.assertEqual(result['image_kind'],'generated')
