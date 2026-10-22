import asyncio,json,unittest,os
from datetime import timedelta
from unittest.mock import AsyncMock,patch
from bs4 import BeautifulSoup
import httpx
from test_editor import article,CURRENT,picture
from test_responsiveness import bot
from app import sources,policy,ai,formatting
from fake_db import Database


def event(**changes):
    data=dict(article(),route='events',category='events',title_fi='Tapahtuma',text_fi='Tervetuloa',
              event={'start':CURRENT+timedelta(days=2),'city':'Helsinki','venue':'Kirjasto'})
    data.update(changes);return data

class ChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database=Database()
        self.patches=[patch.object(bot,'db',self.database),patch.object(bot.firestore,'transactional',self.database.transactional)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        bot.ROUTE.set('news'); ai.COOLDOWNS.clear()
        bot.app.state.gemini_client=object()
    def save(self,id,item): self.database.collection('items').document(id).set(item)

    async def test_manual_publish_routes_event_by_item_not_selected_menu(self):
        self.save('event',event())
        with patch.object(bot,'now',return_value=CURRENT),patch.object(bot,'tg',AsyncMock(return_value={'message_id':1})) as tg:
            self.assertTrue(await bot.publish('event'))
        payload=tg.call_args.args[1]
        self.assertEqual(payload['chat_id'],bot.EVENTS_CHANNEL_ID)
        self.assertIn('#eventhsl',payload['text'])
        self.assertEqual(payload['parse_mode'],'HTML')

    async def test_channels_have_independent_automatic_limits(self):
        self.save('news',article());self.save('event',event())
        for route in ('news','events'):
            self.database.collection('settings').document(bot.route_key('editor',route)).set({'value':{'daily_limit':1,'interval_minutes':0}})
            await bot.put_setting(bot.route_key('autopublish',route),True)
        with patch.object(bot,'now',return_value=CURRENT),patch.object(bot,'tg',AsyncMock(return_value={'message_id':1})):
            self.assertEqual(await asyncio.gather(bot.publish('news',True),bot.publish('event',True)),[True,True])
        self.assertEqual(self.database.data['settings','publication_ledger']['count'],1)
        self.assertEqual(self.database.data['settings','publication_ledger_events']['count'],1)

    async def test_events_auto_starts_off_even_when_news_is_on(self):
        self.save('event',event());await bot.put_setting('autopublish',True)
        with patch.object(bot,'now',return_value=CURRENT),patch.object(bot,'tg',AsyncMock()) as tg:
            self.assertFalse(await bot.publish('event',True))
            tg.assert_not_awaited()

    async def test_queue_filters_before_pagination(self):
        for n in range(10):self.save('news'+str(n),article())
        self.save('event',event())
        self.assertEqual([d.id for d in await bot.item_docs('pending','events',limit=1)],['event'])

    async def test_event_requires_event_channel_user_permission(self):
        self.save('event',event())
        with patch.object(bot,'admin_access',AsyncMock(return_value=False)) as access:
            self.assertFalse(await bot.authorize_item(123,'event'))
        access.assert_awaited_once_with(123,'events')

    async def test_missing_past_and_cancelled_events_never_publish_even_manually(self):
        for data in ({},{'start':CURRENT-timedelta(hours=1),'venue':'Hall'}, {'start':CURRENT+timedelta(days=1),'venue':'Hall','cancelled':True}):
            self.save('event',event(event=data))
            with patch.object(bot,'now',return_value=CURRENT),patch.object(bot,'tg',AsyncMock()) as tg:
                self.assertFalse(await bot.publish('event'))
                tg.assert_not_awaited()

    async def test_source_change_requires_review_before_publishing(self):
        item=event();self.save('event',item)
        fresh=dict(item,event=dict(item['event'],cancelled=True))
        with patch.object(bot,'now',return_value=CURRENT),patch.object(sources,'refresh_event',AsyncMock(return_value=fresh)),patch.object(bot,'tg',AsyncMock()) as tg:
            self.assertFalse(await bot.publish('event'));tg.assert_not_awaited()
        self.assertTrue(self.database.data['items','event']['event']['cancelled'])

    async def test_event_found_in_news_goes_to_events_queue_without_publishing(self):
        item=dict(article(),route='news');self.save('news',item)
        ai_output={'title':'Event','body':'Facts','category':'events','sensitive':False,'has_missing_event_details':True}
        with patch.object(bot,'ai_prepare',AsyncMock(return_value=ai_output)),patch.object(bot,'prepare_image',AsyncMock()) as images:
            result=await bot.prepare_item(self.database.collection('items').document('news'),item,policy.config())
        self.assertEqual(result['status'],'routed');images.assert_not_awaited()
        queued=[v for (name,_),v in self.database.data.items() if name=='items' and v.get('status')=='queued']
        self.assertEqual(len(queued),1);self.assertEqual(queued[0]['route'],'events')

    async def test_catalog_migration_preserves_disabled_sources_and_repeats_safely(self):
        self.database.collection('sources').document('yle-news-en').set({'enabled':False,'name':'Custom'})
        self.assertGreater(await bot.seed_catalog(),20)
        self.assertEqual(await bot.seed_catalog(),0)
        self.assertFalse(self.database.data['sources','yle-news-en']['enabled'])

    async def test_settings_and_image_budgets_are_per_channel(self):
        await bot.change_config('daily_limit',7)
        bot.ROUTE.set('events');await bot.change_config('daily_limit',19)
        self.assertEqual((await bot.editor_config('news'))['daily_limit'],7)
        self.assertEqual((await bot.editor_config('events'))['daily_limit'],19)
        for route in ('news','events'):
            cfg=await bot.editor_config(route);cfg['image_daily_limit']=1
            self.assertTrue(await bot.reserve_image(cfg));self.assertFalse(await bot.reserve_image(cfg))

    async def test_slow_event_collection_does_not_block_news_lease(self):
        entered,release=asyncio.Event(),asyncio.Event()
        async def batch():
            if bot.ROUTE.get()=='events':entered.set();await release.wait()
            return {'route':bot.ROUTE.get()}
        with patch.object(bot,'collect_batch',AsyncMock(side_effect=batch)):
            task=asyncio.create_task(bot.collect('events'));await entered.wait()
            self.assertEqual((await bot.collect('news'))['route'],'news')
            release.set();await task

class AdapterTests(unittest.TestCase):
    def test_catalog_has_at_least_twenty_news_feeds(self):
        self.assertGreaterEqual(sum(s['route']=='news' and s['enabled'] for s in bot.CATALOG.values()),20)
    def test_canonical_dedup_is_cross_source_and_separate_by_channel(self):
        a=dict(article(),route='news',source_id='one');b=dict(a,source_id='two',url=a['url']+'?utm_source=fb')
        self.assertEqual(sources.identity(a),sources.identity(b))
        self.assertNotEqual(sources.identity(a),sources.identity(dict(a,route='events')))
    def test_linked_normalization_keeps_verified_dates_location_and_credit(self):
        raw={'id':'foo','@id':'https://api.hel.fi/linkedevents/v1/event/foo/','name':{'fi':'Test'},'start_time':'2026-11-01T12:00:00Z','event_status':'EventCancelled',
             'location':{'name':{'fi':'Library'},'address_locality':{'fi':'Espoo'}},'images':[{'url':'https://example.org/p.jpg','photographer_name':'Photo author','license':'event_only'}], 'offers':[{'is_free':True}]}
        item=sources.linked_entry(raw,{})
        self.assertEqual(item['event']['city'],'Espoo');self.assertTrue(item['event']['cancelled'])
        self.assertTrue(item['event']['is_free']);self.assertEqual(item['image_credit'],'Photo author')
        self.assertEqual(item['event']['start'].year,2026)
    def test_community_feed_without_date_cannot_pass_auto_rules(self):
        xml=b'<rss version="2.0"><channel><item><title>Event</title><link>https://example.org/event</link></item></channel></rss>'
        row=sources.rss_entries(xml,{'route':'events'},CURRENT)[0]
        item=dict(row,route='events',category='events',text='Hello',sensitive=False)
        self.assertEqual(policy.automatic_reason(item,policy.config(route='events'),CURRENT,{}),'Нет подтверждённой даты события')
    def test_bilingual_html_caption_escapes_content_and_always_keeps_channel_tag(self):
        cfg=policy.config({'hashtags':False,'signature':'S'*100},route='events')
        item=event(title='<b>Hack</b>'+'😀'*90,text='Український текст '*200,title_fi='Suomen otsikko',text_fi='Suomeksi '*300,image_kind='generated')
        html=bot.format_post(item,cfg,photo=True);visible=BeautifulSoup(html,'html.parser').get_text()
        self.assertLessEqual(formatting.units(visible),1024)
        self.assertIn('&lt;b&gt;Hack&lt;/b&gt;',html);self.assertIn('Suomen otsikko',visible)
        self.assertIn('Ілюстрація ШІ',visible);self.assertIn('#eventhsl',visible)
        self.assertNotIn('#Події',visible)
    def test_city_and_free_filters_handle_unknown_data(self):
        cfg=policy.config({'free_only':True},route='events')
        self.assertFalse(policy.topic_allowed(event(),cfg))
        self.assertTrue(policy.topic_allowed(event(event={'city':'Helsinki','is_free':True}),cfg))
        self.assertFalse(policy.topic_allowed(event(event={'city':'Turku','is_free':True}),cfg))

class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self): ai.COOLDOWNS.clear()
    async def test_busy_provider_is_skipped_until_cooldown_expires(self):
        valid={'title':'News','body':'Facts','category':'news','sensitive':False,'has_missing_event_details':False}
        calls=[]
        response=httpx.Response(429,headers={'Retry-After':'60'},request=httpx.Request('POST','https://api.example'))
        async def request(client,provider,model,prompt):
            calls.append(provider)
            if provider=='gemini':raise httpx.HTTPStatusError('busy',request=response.request,response=response)
            return valid
        with patch.object(ai,'available',return_value={'gemini':True,'openai':True}),patch.object(ai,'request_json',AsyncMock(side_effect=request)):
            cfg=policy.config({'ai_order':['gemini','openai']})
            for _ in range(2):self.assertEqual((await ai.prepare(None,'Title','Facts','https://example.org','news',cfg,bot.CATEGORIES))['ai_provider'],'openai')
        self.assertEqual(calls,['gemini','openai','openai'])
    async def test_new_provider_endpoints_use_only_fixed_hosts(self):
        for provider,host in [('deepseek','api.deepseek.com'),('mistral','api.mistral.ai'),('openrouter','openrouter.ai')]:
            def respond(request):
                self.assertEqual(request.url.host,host);self.assertEqual(request.headers['Authorization'],'Bearer fixture')
                return httpx.Response(200,json={'choices':[{'message':{'content':'{"approved":true}'}}]})
            with patch.dict(os.environ,{provider.upper()+'_API_KEY':'fixture'}):
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    self.assertTrue((await ai.request_json(client,provider,policy.DEFAULTS[provider+'_model'],'JSON'))['approved'])

if __name__=='__main__':unittest.main()

class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_recheck_expands_location(self):
        raw={'@id':'https://api.hel.fi/linkedevents/v1/event/test/','name':{'fi':'Event'},
             'start_time':'2026-11-01T12:00:00Z','location':{'name':{'fi':'Hall'},'address_locality':{'fi':'Helsinki'}}}
        with patch.object(sources.media,'fetch',AsyncMock(return_value=(json.dumps(raw).encode(),raw['@id']))) as fetch:
            fresh=await sources.refresh_event(event(event={'api_url':raw['@id']}))
        self.assertIn('include=location',fetch.call_args.args[0])
        self.assertEqual(fresh['event']['venue'],'Hall')
    async def test_adapter_keeps_future_records_and_advances_pagination(self):
        raw={'meta':{'count':90,'next':'https://api.hel.fi/linkedevents/v1/event/?page=2'},'data':[
            {'@id':'https://example.org/future','name':{'fi':'Future'},'start_time':(CURRENT+timedelta(days=1)).isoformat()},
            {'@id':'https://example.org/past','name':{'fi':'Past'},'start_time':(CURRENT-timedelta(days=1)).isoformat()}]}
        with patch.object(sources.media,'fetch',AsyncMock(return_value=(json.dumps(raw).encode(),'https://example.org/api'))) as fetch:
            rows,progress=await sources.fetch({'kind':'linked','url':'https://example.org/api','next_page':1},policy.config(route='events'),CURRENT)
        self.assertEqual(len(rows),1);self.assertEqual(progress['next_page'],2)
        self.assertEqual(fetch.call_args.kwargs['types'],('application/json',))
        self.assertNotIn('starts_after',fetch.call_args.args[0])
