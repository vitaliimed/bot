import asyncio
import base64
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from test_responsiveness import bot
from fake_db import Database
from app import ai, media, policy

CURRENT = datetime(2026,10,21,10,tzinfo=timezone.utc)

def article(**extra):
    return dict(status='pending', title='Новина', text='Перевірені факти', url='https://example.org/news',
                category='news', sensitive=False, missing_details=False, created_at=CURRENT, **extra)

def picture():
    out = io.BytesIO()
    Image.new('RGB',(800,450),(40,100,180)).save(out,'PNG')
    return out.getvalue()

class PolicyTests(unittest.TestCase):
    def test_defaults_are_not_mutated_by_category_buttons(self):
        cfg = policy.config()
        cfg['categories'].remove('sport')
        self.assertIn('sport',policy.config()['categories'])

    def test_validation_rejects_bad_limits_timezones_and_provider_endpoints(self):
        for key,value in [('daily_limit','-1'),('batch_size','1000'),('window_start','25:00'),
                          ('timezone','Not/AZone'),('ai_order','https://evil.example'),('image_model','x?key=secret')]:
            with self.subTest(key=key), self.assertRaises(ValueError): policy.validate(key,value)

    def test_category_and_keyword_filters(self):
        cfg = policy.config({'categories':['news'],'exclude_words':'хокей,футбол'})
        self.assertFalse(policy.topic_allowed(dict(article(),category='sport'),cfg))
        self.assertFalse(policy.topic_allowed(dict(article(),title='ФУТБОЛ у Фінляндії'),cfg))
        cfg['include_words'] = 'Кела,Фінляндії'
        self.assertTrue(policy.topic_allowed(dict(article(),title='Допомога КЕЛА'),cfg))
        self.assertFalse(policy.topic_allowed(article(),cfg))

    def test_time_window_and_overnight_windows(self):
        cfg = policy.config({'window_start':'22:00','window_end':'06:00','timezone':'UTC'})
        self.assertEqual(policy.automatic_reason(article(),cfg,CURRENT,{}),'Вне времени публикации')
        late = CURRENT.replace(hour=23)
        self.assertEqual(policy.automatic_reason(article(),cfg,late,{}),'')
        cfg['weekdays'] = [1]
        self.assertEqual(policy.automatic_reason(article(),cfg,late,{}),'Сегодня публикации отключены')

    def test_quota_interval_expiry_and_review_fail_closed(self):
        cfg = policy.config()
        for ledger,expected in [({'day':'2026-10-21','count':12},'Дневной лимит'),
                                ({'last_at':CURRENT-timedelta(minutes=10)},'Интервал между публикациями')]:
            self.assertEqual(policy.automatic_reason(article(),cfg,CURRENT,ledger),expected)
        self.assertEqual(policy.automatic_reason(dict(article(),source_published_at=CURRENT-timedelta(days=3)),cfg,CURRENT,{}),'Новость устарела')
        cfg['review_provider'] = 'groq'
        self.assertEqual(policy.automatic_reason(article(),cfg,CURRENT,{}),'Нет одобрения проверяющего ИИ')
        self.assertEqual(policy.automatic_reason(dict(article(),review_passed=True,review_provider='groq'),cfg,CURRENT,{}),'')

class AITests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_writer_flags_fall_back_to_other_provider(self):
        calls = []
        valid = {'title':'Новина','body':'Факти','category':'news','sensitive':False,'has_missing_event_details':False}
        async def request(client,provider,model,prompt):
            calls.append(provider)
            return dict(valid,sensitive='false') if provider == 'gemini' else valid
        with patch.object(ai,'available',return_value={'gemini':True,'groq':True}), \
             patch.object(ai,'request_json',AsyncMock(side_effect=request)):
            result = await ai.prepare(None,'Title','Facts','https://example.org','news',policy.config(),bot.CATEGORIES)
        self.assertEqual(calls,['gemini','groq'])
        self.assertEqual(result['ai_provider'],'groq')

    async def test_unavailable_reviewer_does_not_approve(self):
        valid = {'title':'Title','body':'Facts','category':'news','sensitive':False,'has_missing_event_details':False}
        with patch.object(ai,'available',return_value={'gemini':True}), \
             patch.object(ai,'request_json',AsyncMock(side_effect=[valid,TimeoutError()])):
            result = await ai.prepare(None,'Title','Facts','https://example.org','news',
                                      policy.config({'review_provider':'groq'}),bot.CATEGORIES)
        self.assertFalse(result['review_passed'])

    async def test_openai_compatible_request_uses_secret_header_and_json_mode(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"approved":true}'}}]})
        with patch.dict(os.environ,{'GROQ_API_KEY':'fixture'}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                self.assertEqual(await ai.request_json(client,'groq','openai/gpt-oss-20b','JSON please'),{'approved':True})
        request = requests[0]
        self.assertEqual(request.headers['Authorization'],'Bearer fixture')
        self.assertNotIn('fixture',str(request.url))
        self.assertEqual(json.loads(request.content)['response_format'],{'type':'json_object'})

class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_image_request_parses_base64_and_uses_bounded_quality(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200,json={'data':[{'b64_json':base64.b64encode(picture()).decode()}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await media.generate_openai(client,article(),'gpt-image-2.5-flare','fixture-key','low')
        request = requests[0]
        body = json.loads(request.content)
        self.assertEqual(body['n'],1)
        self.assertEqual(body['quality'],'low')
        self.assertNotIn('response_format',body)
        self.assertEqual(request.headers['Authorization'],'Bearer fixture-key')
        self.assertEqual(result['image_provider'],'openai')
        self.assertLessEqual(len(result['image_bytes']),450_000)

    def test_rss_and_open_graph_candidates(self):
        entry = {'media_content':[{'url':'/image.jpg','type':'image/jpeg'}],
                 'enclosures':[{'href':'https://example.org/video.mp4','type':'video/mp4'}]}
        self.assertEqual(media.candidates(entry,'https://example.org/news'),['https://example.org/image.jpg'])
        self.assertEqual(media.page_images(b'<meta property="og:image" content="/photo.jpg">','https://example.org/news'),['https://example.org/photo.jpg'])

    async def test_private_dns_addresses_are_rejected(self):
        for ip in ('127.0.0.1','10.0.0.1','169.254.169.254','::1','fd00::1'):
            with self.subTest(ip=ip), patch.object(media.socket,'getaddrinfo',return_value=[(None,None,None,None,(ip,443))]), self.assertRaises(ValueError):
                await media.public_address('example.org')

    async def test_download_pins_ip_and_preserves_tls_hostname(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200,content=b'photo',headers={'content-type':'image/jpeg'})
        real_client = httpx.AsyncClient
        with patch.object(media,'public_address',AsyncMock(return_value='8.8.8.8')), \
             patch.object(media.httpx,'AsyncClient',side_effect=lambda **kw:real_client(transport=httpx.MockTransport(respond),**kw)):
            data,url = await media.fetch('https://example.org/photo',limit=10,types=('image/',))
        self.assertEqual(data,b'photo')
        self.assertEqual(requests[0].url.host,'8.8.8.8')
        self.assertEqual(requests[0].headers['host'],'example.org')
        self.assertEqual(requests[0].extensions['sni_hostname'],'example.org')

    async def test_redirect_to_private_ip_and_oversized_download_are_rejected(self):
        real_client = httpx.AsyncClient
        def respond(request): return httpx.Response(302,headers={'location':'https://169.254.169.254/latest'})
        with patch.object(media,'public_address',AsyncMock(side_effect=['8.8.8.8',ValueError('private')])), \
             patch.object(media.httpx,'AsyncClient',side_effect=lambda **kw:real_client(transport=httpx.MockTransport(respond),**kw)), self.assertRaises(ValueError):
            await media.fetch('https://example.org/photo',limit=10,types=('image/',))
        def large(request): return httpx.Response(200,content=b'x'*20,headers={'content-type':'image/jpeg'})
        with patch.object(media,'public_address',AsyncMock(return_value='8.8.8.8')), \
             patch.object(media.httpx,'AsyncClient',side_effect=lambda **kw:real_client(transport=httpx.MockTransport(large),**kw)), self.assertRaises(ValueError):
            await media.fetch('https://example.org/photo',limit=10,types=('image/',))

    async def test_generation_parses_inline_image_and_normalizes_to_jpeg(self):
        def respond(request):
            return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'inlineData':{'mimeType':'image/png','data':base64.b64encode(picture()).decode()}}]}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await media.generate(client,article(),'fixture-image-model','fixture-key')
        self.assertEqual(result['image_kind'],'generated')
        self.assertLessEqual(len(result['image_bytes']),450_000)
        self.assertEqual(Image.open(io.BytesIO(result['image_bytes'])).format,'JPEG')

    async def test_article_image_is_fallback_after_broken_rss_image(self):
        with patch.object(media,'fetch',AsyncMock(side_effect=[ValueError(),
                   (b'<meta property="og:image" content="/photo.jpg">','https://example.org/news'),
                   (picture(),'https://example.org/photo.jpg')])):
            result = await media.source_image('https://example.org/news',['https://example.org/broken.jpg'])
        self.assertEqual(result['image_origin'],'https://example.org/photo.jpg')

class EditorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.app.state.gemini_client = object()
        self.database = Database()
        self.db_patch = patch.object(bot,'db',self.database)
        self.tx_patch = patch.object(bot.firestore,'transactional',self.database.transactional)
        self.db_patch.start(); self.tx_patch.start()
        self.addCleanup(self.db_patch.stop); self.addCleanup(self.tx_patch.stop)

    def save(self,id,item): self.database.collection('items').document(id).set(item)

    async def test_settings_updates_merge_and_unauthorized_commands_cannot_write(self):
        await bot.change_config('daily_limit','5')
        await bot.change_config('interval_minutes','90')
        cfg = await bot.editor_config()
        self.assertEqual((cfg['daily_limit'],cfg['interval_minutes']),(5,90))
        with patch.object(bot,'admin_access',AsyncMock(return_value=False)), patch.object(bot,'say',AsyncMock()):
            await bot.handle_message({'from':{'id':123},'chat':{'id':123,'type':'private'},'text':'/set daily_limit 99'})
        self.assertEqual((await bot.editor_config())['daily_limit'],5)

    async def test_competing_automatic_publishers_share_atomic_daily_quota(self):
        self.save('one',article()); self.save('two',article())
        cfg = policy.config({'daily_limit':1,'interval_minutes':0})
        await bot.put_setting('autopublish',True)
        await bot.change_config('daily_limit',1)
        await bot.change_config('interval_minutes',0)
        with patch.object(bot,'now',return_value=CURRENT), patch.object(bot,'say',AsyncMock(return_value={'message_id':10})) as send:
            results = await asyncio.gather(bot.publish('one',True,cfg),bot.publish('two',True,cfg))
        self.assertEqual(sum(results),1)
        send.assert_awaited_once()

    async def test_photo_send_failure_is_not_retried_as_text(self):
        self.save('one',article(image_bytes=picture(),image_kind='generated'))
        with patch.object(bot,'tg',AsyncMock(side_effect=TimeoutError())) as tg, patch.object(bot,'say',AsyncMock()) as say:
            self.assertFalse(await bot.publish('one',cfg=policy.config()))
        tg.assert_awaited_once()
        self.assertEqual(tg.call_args.args[0],'sendPhoto')
        say.assert_not_awaited()
        self.assertEqual(self.database.data['items','one']['status'],'needs_reconciliation')

    async def test_successful_photo_keeps_file_id_and_releases_stored_bytes(self):
        self.save('one',article(image_bytes=picture(),image_kind='generated'))
        with patch.object(bot,'tg',AsyncMock(return_value={'message_id':7,'photo':[{'file_id':'cached'}]})) as send:
            self.assertTrue(await bot.publish('one',cfg=policy.config()))
        item = self.database.data['items','one']
        self.assertEqual(item['image_file_id'],'cached')
        self.assertNotIn('image_bytes',item)
        self.assertIn('Ілюстрація ШІ',send.call_args.args[1]['caption'])
        self.assertIn('https://example.org/news',send.call_args.args[1]['caption'])

    async def test_image_attempt_budget_is_atomic_even_on_generation_failure(self):
        cfg = policy.config({'image_daily_limit':1,'image_mode':'generate'})
        with patch.object(bot.media,'generate',AsyncMock(side_effect=ValueError('unavailable'))) as generate:
            results = await asyncio.gather(bot.prepare_image(article(),cfg),bot.prepare_image(article(),cfg))
        generate.assert_awaited_once()
        self.assertTrue(any(result.get('image_error') for result in results))

    async def test_openai_image_failure_uses_gemini_and_counts_each_attempt(self):
        cfg = policy.config({'image_daily_limit':2,'image_mode':'generate'})
        expected = {'image_bytes':picture(),'image_kind':'generated','image_provider':'gemini'}
        with patch.dict(os.environ,{'OPENAI_API_KEY':'fixture'}), \
             patch.object(bot.media,'generate_openai',AsyncMock(side_effect=ValueError('unavailable'))) as openai, \
             patch.object(bot.media,'generate',AsyncMock(return_value=expected)) as gemini:
            self.assertEqual(await bot.prepare_image(article(),cfg),expected)
        openai.assert_awaited_once(); gemini.assert_awaited_once()
        self.assertEqual(self.database.data['settings','image_ledger']['count'],2)

    async def test_collector_lease_rejects_overlapping_requests(self):
        entered,release = asyncio.Event(),asyncio.Event()
        async def batch():
            entered.set(); await release.wait(); return {'prepared':1}
        with patch.object(bot,'collect_batch',AsyncMock(side_effect=batch)):
            first = asyncio.create_task(bot.collect())
            await entered.wait()
            self.assertEqual(await bot.collect(),{'busy':True})
            release.set()
            self.assertEqual(await first,{'prepared':1})
        self.assertNotIn(('settings','collector_lease'),self.database.data)

    async def test_full_collect_prepares_photo_and_skips_disabled_sport_topic(self):
        for id,category in [('news','news'),('sport','sport')]:
            self.database.collection('sources').document(id).set({'url':'https://example.org/'+id,'category':category,'enabled':True})
        await bot.change_config('categories',['news'])
        xml = b'<rss version="2.0"><channel><item><guid>1</guid><title>News</title><link>https://example.org/article</link><description>Facts</description></item></channel></rss>'
        draft = {'title':'News','body':'Facts','category':'news','sensitive':False,'has_missing_event_details':False}
        with patch.object(bot.media,'fetch',AsyncMock(return_value=(xml,'https://example.org/news'))) as fetch, \
             patch.object(bot,'ai_prepare',AsyncMock(return_value=draft)), \
             patch.object(bot,'prepare_image',AsyncMock(return_value={'image_bytes':picture(),'image_kind':'source'})):
            stats = await bot.collect()
        self.assertEqual(stats['prepared'],1)
        fetch.assert_awaited_once()
        item = next(v for (name,id),v in self.database.data.items() if name=='items')
        self.assertEqual(item['status'],'pending')
        self.assertEqual(item['image_kind'],'source')

    async def test_existing_pending_queue_is_published_without_new_feed_items(self):
        self.save('old',article())
        self.database.collection('sources').document('disabled').set({'enabled':False})
        await bot.put_setting('autopublish',True)
        with patch.object(bot,'now',return_value=CURRENT), patch.object(bot,'say',AsyncMock(return_value={'message_id':1})):
            stats = await bot.collect()
        self.assertEqual(stats['published'],1)

    async def test_disabled_automatic_mode_and_sending_state_cannot_be_overridden(self):
        self.save('one',article())
        with patch.object(bot,'now',return_value=CURRENT), patch.object(bot,'say',AsyncMock()) as send:
            self.assertFalse(await bot.publish('one',automatic=True,cfg=policy.config()))
            send.assert_not_awaited()
        ref = self.database.collection('items').document('one')
        ref.update({'status':'sending'})
        self.assertFalse(await bot.transition_item(ref,{'status':'awaiting_ai'},('pending','awaiting_ai')))
        self.assertEqual(ref.get().to_dict()['status'],'sending')

    async def test_failed_ai_is_retried_and_does_not_stay_processing_forever(self):
        self.database.collection('sources').document('disabled').set({'enabled':False})
        failed = dict(article(),status='awaiting_ai',attempts=1,summary='Source facts')
        self.save('retry',failed)
        draft = {'title':'News','body':'Facts','category':'news','sensitive':False,'has_missing_event_details':False}
        with patch.object(bot,'ai_prepare',AsyncMock(return_value=draft)), patch.object(bot,'prepare_image',AsyncMock(return_value={})):
            stats = await bot.collect()
        self.assertEqual(stats['prepared'],1)
        self.assertEqual(self.database.data['items','retry']['status'],'pending')
        self.assertEqual(self.database.data['items','retry']['attempts'],2)

    def test_photo_caption_fits_utf16_and_preserves_credit(self):
        item = dict(article(),title='🇫🇮'*150,text='Довгий текст '*300,image_kind='generated')
        text = bot.format_post(item,policy.config(),photo=True)
        self.assertLessEqual(len(text.encode('utf-16-le'))//2,1024)
        self.assertIn('Ілюстрація ШІ',text)
        self.assertIn(item['url'],text)

if __name__ == '__main__': unittest.main()
