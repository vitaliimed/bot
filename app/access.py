"""Authorize bot management through an explicit ID or current channel rights."""
import logging

log = logging.getLogger('newsbot.access')


async def can_manage(user_id, *, mode, admin_id, channel_id, telegram):
    if type(user_id) is not int or user_id <= 0:
        return False
    if mode == 'id':
        return admin_id > 0 and user_id == admin_id
    if mode != 'channel_admins':
        return False
    try:
        member = await telegram('getChatMember', {
            'chat_id': channel_id, 'user_id': user_id,
        })
    except Exception as error:
        log.warning('Channel access verification failed (%s)', type(error).__name__)
        return False
    if not isinstance(member, dict):
        return False
    actor = member.get('user')
    if not isinstance(actor, dict) or actor.get('id') != user_id or actor.get('is_bot'):
        return False
    return member.get('status') == 'creator' or (
        member.get('status') == 'administrator'
        and member.get('can_post_messages') is True
    )
