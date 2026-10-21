import asyncio
import importlib
import json
import os
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx


with patch.dict(os.environ, {
    'BOT_TOKEN': 'test-token', 'CHANNEL_ID': '@test_channel',
    'WEBHOOK_SECRET': 'test-webhook', 'CRON_SECRET': 'test-cron',
    'ADMIN_ID': '0', 'ADMIN_AUTH_MODE': 'channel_admins',
    'GEMINI_API_KEY': 'test-key',
}), patch('google.cloud.firestore.Client'):
    bot = importlib.import_module('app.main')


class ResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    user_id = 123456789

    def message(self, text):
        return {'from': {'id': self.user_id}, 'chat': {'id': self.user_id, 'type': 'private'}, 'text': text}

    def callback(self, data):
        return {'id': 'callback-fixture', 'from': {'id': self.user_id},
                'message': {'chat': {'id': self.user_id, 'type': 'private'}}, 'data': data}

    async def test_setting_read_does_not_block_other_requests(self):
        started, release = threading.Event(), threading.Event()

        def slow_read():
            started.set()
            release.wait(timeout=2)
            return SimpleNamespace(exists=True, to_dict=lambda: {'value': True})

        database = MagicMock()
        database.collection.return_value.document.return_value.get.side_effect = slow_read
        with patch.object(bot, 'db', database):
            task = asyncio.create_task(bot.setting('autopublish', False))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                self.assertFalse(task.done())
                self.assertEqual(await asyncio.wait_for(bot.health(), timeout=0.2), {'ok': True})
            finally:
                release.set()
                self.assertTrue(await task)

    async def test_query_iteration_runs_outside_the_request_thread(self):
        main_thread = threading.get_ident()
        worker_threads = []

        def stream():
            worker_threads.append(threading.get_ident())
            yield 'document'

        self.assertEqual(await bot.documents(SimpleNamespace(stream=stream)), ['document'])
        self.assertNotEqual(worker_threads, [main_thread])

    async def test_commands_skip_edit_state_lookup(self):
        for command, handler in (('/start', 'dashboard'), ('/help', 'show_help')):
            with self.subTest(command=command), ExitStack() as stack:
                access = stack.enter_context(patch.object(bot, 'admin_access', AsyncMock(return_value=True)))
                setting = stack.enter_context(patch.object(bot, 'setting', AsyncMock()))
                output = stack.enter_context(patch.object(bot, handler, AsyncMock()))
                await bot.handle_message(self.message(command))
                access.assert_awaited_once_with(self.user_id)
                setting.assert_not_awaited()
                output.assert_awaited_once_with(self.user_id)

    async def test_help_and_stats_buttons_acknowledge_before_one_access_check(self):
        for data, handler in (('help', 'show_help'), ('stats', 'show_stats')):
            events = []

            async def acknowledge(method, payload):
                events.append('acknowledged')

            async def access(user_id):
                events.append('authorized')
                return True

            with self.subTest(data=data), ExitStack() as stack:
                telegram = stack.enter_context(patch.object(bot, 'tg', AsyncMock(side_effect=acknowledge)))
                permission = stack.enter_context(patch.object(bot, 'admin_access', AsyncMock(side_effect=access)))
                output = stack.enter_context(patch.object(bot, handler, AsyncMock()))
                await bot.handle_callback(self.callback(data))
                self.assertEqual(events, ['acknowledged', 'authorized'])
                permission.assert_awaited_once_with(self.user_id)
                telegram.assert_awaited_once_with('answerCallbackQuery', {'callback_query_id': 'callback-fixture'})
                output.assert_awaited_once_with(self.user_id)

    async def test_early_acknowledgement_does_not_grant_permission(self):
        with patch.object(bot, 'tg', AsyncMock()), \
             patch.object(bot, 'admin_access', AsyncMock(return_value=False)), \
             patch.object(bot, 'put_setting', AsyncMock()) as write, \
             patch.object(bot, 'say', AsyncMock()) as say:
            await bot.handle_callback(self.callback('toggle'))
            write.assert_not_awaited()
            say.assert_awaited_once_with(self.user_id, bot.access_hint())

    async def test_telegram_calls_reuse_the_open_client(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'ok': True, 'result': True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            bot.app.state.telegram_client = client
            self.assertTrue(await bot.tg('answerCallbackQuery', {'callback_query_id': 'first'}))
            self.assertTrue(await bot.tg('answerCallbackQuery', {'callback_query_id': 'second'}))
            self.assertFalse(client.is_closed)
            self.assertEqual(len(requests), 2)

    async def test_clients_close_when_application_stops(self):
        proxy_keys = ('ALL_PROXY', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'http_proxy', 'https_proxy')
        with patch.dict(os.environ, {key: '' for key in proxy_keys}):
            async with bot.lifespan(bot.app):
                telegram = bot.app.state.telegram_client
                gemini = bot.app.state.gemini_client
                self.assertFalse(telegram.is_closed)
                self.assertFalse(gemini.is_closed)
        self.assertTrue(telegram.is_closed)
        self.assertTrue(gemini.is_closed)

    async def test_gemini_uses_its_existing_client(self):
        requests = []

        def respond(request):
            requests.append(request)
            body = json.dumps({'title': 'Test', 'body': 'Summary', 'category': 'news',
                               'sensitive': False, 'has_missing_event_details': False})
            return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': body}]}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            bot.app.state.gemini_client = client
            for _ in range(2):
                with patch.dict(os.environ, {'GEMINI_API_KEY':'test-key'}):
                    self.assertEqual((await bot.ai_prepare('Title', 'Facts', 'https://example.org/news', 'news', bot.policy.config()))['body'], 'Summary')
            self.assertFalse(client.is_closed)
            self.assertEqual(len(requests), 2)

    async def test_telegram_rate_limit_retry_is_bounded_and_respects_delay(self):
        responses = iter([
            httpx.Response(429, json={'ok': False, 'parameters': {'retry_after': 2}}),
            httpx.Response(200, json={'ok': True, 'result': True}),
        ])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: next(responses))) as client:
            bot.app.state.telegram_client = client
            with patch.object(bot.asyncio, 'sleep', AsyncMock()) as sleep:
                self.assertTrue(await bot.tg('sendMessage', {'chat_id': 1, 'text': 'fixture'}))
                sleep.assert_awaited_once_with(2)

    async def test_expired_button_is_ignored_without_retrying_its_action(self):
        response = httpx.Response(400, json={'description': 'Bad Request: query is too old and response timeout expired'},
                                  request=httpx.Request('POST', 'https://api.telegram.org/test/answerCallbackQuery'))
        error = httpx.HTTPStatusError('expired', request=response.request, response=response)
        with patch.object(bot, 'tg', AsyncMock(side_effect=error)), \
             patch.object(bot, 'admin_access', AsyncMock()) as access, \
             patch.object(bot, 'put_setting', AsyncMock()) as write:
            await bot.handle_callback(self.callback('toggle'))
            access.assert_not_awaited()
            write.assert_not_awaited()

    async def test_collection_still_prepares_a_new_item_for_review(self):
        source = SimpleNamespace(id='feed', to_dict=lambda: {
            'enabled': True, 'url': 'https://example.org/rss', 'category': 'news',
        })
        sources = MagicMock()
        sources.limit.return_value = sources
        sources.stream.return_value = [source]
        item = MagicMock()
        item.get.return_value = SimpleNamespace(exists=False)
        items = MagicMock()
        items.document.return_value = item
        database = MagicMock()
        database.collection.side_effect = lambda name: {'sources': sources, 'items': items}[name]
        xml = b'<rss version="2.0"><channel><title>Feed</title><item><guid>fixture</guid><title>News</title><link>https://example.org/news</link><description>Facts</description></item></channel></rss>'
        prepared = {'title': 'Title', 'body': 'Summary', 'category': 'news',
                    'sensitive': False, 'has_missing_event_details': False}
        with patch.object(bot, 'db', database), \
             patch.object(bot.media, 'fetch', AsyncMock(return_value=(xml,'https://example.org/rss'))), \
             patch.object(bot, 'editor_config', AsyncMock(return_value=bot.policy.config({'batch_size':1,'image_mode':'off'}))), \
             patch.object(bot, 'ai_prepare', AsyncMock(return_value=prepared)), \
             patch.object(bot, 'setting', AsyncMock(return_value=False)), \
             patch.object(bot, 'publish', AsyncMock()) as publish:
            stats = await bot.collect_batch()
        self.assertEqual(stats['new'], 1)
        self.assertEqual(stats['prepared'], 1)
        self.assertEqual(stats['errors'], 0)
        self.assertEqual(item.create.call_args.args[0]['status'], 'processing')
        self.assertEqual(item.update.call_args.args[0]['status'], 'pending')
        publish.assert_not_awaited()

    async def test_publication_claim_and_status_writes_run_off_the_request_thread(self):
        events, worker_threads = [], []
        main_thread = threading.get_ident()
        item = MagicMock()
        item.get.return_value = SimpleNamespace(exists=True, to_dict=lambda: {
            'status': 'pending', 'title': 'Title', 'text': 'Summary', 'url': 'https://example.org/news',
        })

        def write(changes):
            events.append(changes['status'])
            worker_threads.append(threading.get_ident())

        item.update.side_effect = write
        transaction = MagicMock()
        transaction.update.side_effect = lambda ref, changes: write(changes)
        database = MagicMock()
        database.collection.return_value.document.return_value = item
        database.transaction.return_value = transaction

        async def send(chat, text):
            events.append('telegram')
            return {'message_id': 42}

        with patch.object(bot, 'db', database), \
             patch.object(bot.firestore, 'transactional', side_effect=lambda function: function), \
             patch.object(bot, 'editor_config', AsyncMock(return_value=bot.policy.config())), \
             patch.object(bot, 'say', AsyncMock(side_effect=send)):
            self.assertTrue(await bot.publish('fixture'))
        self.assertEqual(events, ['sending', 'telegram', 'published'])
        self.assertTrue(all(worker != main_thread for worker in worker_threads))

    async def test_uncertain_telegram_publication_is_not_retried(self):
        item = MagicMock()
        item.get.return_value = SimpleNamespace(exists=True, to_dict=lambda: {
            'status': 'pending', 'title': 'Title', 'text': 'Summary', 'url': 'https://example.org/news',
        })
        database = MagicMock()
        database.collection.return_value.document.return_value = item
        with patch.object(bot, 'db', database), \
             patch.object(bot.firestore, 'transactional', side_effect=lambda function: function), \
             patch.object(bot, 'editor_config', AsyncMock(return_value=bot.policy.config())), \
             patch.object(bot, 'say', AsyncMock(side_effect=TimeoutError('fixture'))) as send, \
             self.assertLogs('newsbot', level='ERROR'):
            self.assertFalse(await bot.publish('fixture'))
        send.assert_awaited_once()
        self.assertEqual(item.update.call_args.args[0]['status'], 'needs_reconciliation')


if __name__ == '__main__':
    unittest.main()
