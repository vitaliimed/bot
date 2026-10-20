import unittest
from unittest.mock import AsyncMock

from app.access import can_manage


class AccessTests(unittest.IsolatedAsyncioTestCase):
    user_id = 123456789

    async def check(self, telegram, mode='channel_admins', admin_id=0, user_id=None):
        return await can_manage(self.user_id if user_id is None else user_id,
                                mode=mode, admin_id=admin_id,
                                channel_id='@suominewsua', telegram=telegram)

    def member(self, status, **rights):
        return {'user': {'id': self.user_id, 'is_bot': False}, 'status': status, **rights}

    async def test_owner_allowed_without_configured_id(self):
        telegram = AsyncMock(return_value=self.member('creator'))
        self.assertTrue(await self.check(telegram))
        telegram.assert_awaited_once_with('getChatMember', {
            'chat_id': '@suominewsua', 'user_id': self.user_id,
        })

    async def test_posting_administrator_allowed(self):
        self.assertTrue(await self.check(AsyncMock(return_value=self.member(
            'administrator', can_post_messages=True))))

    async def test_administrator_without_posting_rights_denied(self):
        for right in (False, None, 'true', 1):
            with self.subTest(right=right):
                telegram = AsyncMock(return_value=self.member('administrator', can_post_messages=right))
                self.assertFalse(await self.check(telegram))

    async def test_subscribers_and_former_members_denied(self):
        for status in ('member', 'restricted', 'left', 'kicked', 'unknown'):
            with self.subTest(status=status):
                self.assertFalse(await self.check(AsyncMock(return_value=self.member(
                    status, can_post_messages=True))))

    async def test_explicit_id_mode_does_not_query_telegram(self):
        telegram = AsyncMock()
        self.assertTrue(await self.check(telegram, mode='id', admin_id=self.user_id))
        self.assertFalse(await self.check(telegram, mode='id', admin_id=self.user_id + 1))
        self.assertFalse(await self.check(telegram, mode='id', admin_id=0))
        telegram.assert_not_awaited()

    async def test_unknown_mode_denied(self):
        telegram = AsyncMock()
        self.assertFalse(await self.check(telegram, mode='unknown', admin_id=self.user_id))
        telegram.assert_not_awaited()

    async def test_invalid_sender_ids_denied(self):
        telegram = AsyncMock()
        for user_id in (None, 0, -1, True, '123456789'):
            with self.subTest(user_id=user_id):
                self.assertFalse(await can_manage(user_id, mode='channel_admins', admin_id=0,
                                                 channel_id='@suominewsua', telegram=telegram))
        telegram.assert_not_awaited()

    async def test_invalid_membership_or_wrong_actor_denied(self):
        cases = [None, {}, {'status': 'creator'},
                 {'status': 'creator', 'user': {'id': self.user_id + 1}},
                 {'status': 'creator', 'user': {'id': self.user_id, 'is_bot': True}}]
        for member in cases:
            with self.subTest(member=member):
                self.assertFalse(await self.check(AsyncMock(return_value=member)))

    async def test_telegram_error_denies_access_without_logging_credentials(self):
        telegram = AsyncMock(side_effect=RuntimeError('secret-fixture-do-not-log'))
        with self.assertLogs('newsbot.access', level='WARNING') as captured:
            self.assertFalse(await self.check(telegram))
        self.assertNotIn('secret-fixture-do-not-log', ''.join(captured.output))

    async def test_revocation_checked_on_next_request(self):
        telegram = AsyncMock(side_effect=[self.member('creator'), self.member('member')])
        self.assertTrue(await self.check(telegram))
        self.assertFalse(await self.check(telegram))
        self.assertEqual(telegram.await_count, 2)


if __name__ == '__main__':
    unittest.main()
