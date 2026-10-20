"""Rights of BOTS in channels and groups (TZ-bot-interaction 4.5), always prepare -> was/now preview -> execute.

Rights of people are out of scope and `add_admins` is never granted: only the owner can do that by hand.
"""
from __future__ import annotations
import secrets

from telegram_backend import BackendError,_operation,_limit,_text,_user
from bot_interaction import UNTRUSTED

RIGHTS=('post_messages','edit_messages','delete_messages','pin_messages','invite_users','change_info','manage_topics','post_stories','edit_stories','delete_stories','ban_users','manage_call')
CHANNEL_RIGHTS={'post_messages','edit_messages','delete_messages','pin_messages','invite_users','change_info','post_stories','edit_stories','delete_stories'}
GROUP_RIGHTS={'delete_messages','pin_messages','invite_users','change_info','manage_topics','ban_users','manage_call'}
ALL_FLAGS=RIGHTS+('add_admins',)


def _tl():
    from telethon.tl import functions,types
    from telethon import utils
    return functions,types,utils


def _blank():
    return {right:False for right in ALL_FLAGS}


class BotAdmin:
    """Mixed into ExtendedBackend."""

    @staticmethod
    def _chat_kind(entity):
        if type(entity).__name__!='Channel':
            raise BackendError('unsupported_operation','Granular administrator rights exist only in channels and supergroups; basic groups are not supported.')
        if getattr(entity,'broadcast',False):
            return 'channel'
        if getattr(entity,'megagroup',False):
            return 'group'
        raise BackendError('unsupported_operation','Select a channel or a supergroup.')

    @staticmethod
    def _check_title(kind,title):
        if title and kind=='channel':
            raise BackendError('invalid_input','Custom administrator titles exist only in supergroups; Telegram does not show them in channels.')

    def _check_rights(self,kind,entity,rights,applicable=True):
        if 'add_admins' in rights:
            raise BackendError('forbidden_right','add_admins is never granted through this server; only the chat owner can do it manually in Telegram.')
        unknown=[right for right in rights if right not in RIGHTS]
        if unknown:
            raise BackendError('invalid_input','Unknown rights: '+', '.join(map(str,unknown))+'. Allowed: '+', '.join(RIGHTS)+'.')
        if not applicable:
            return list(dict.fromkeys(rights))
        allowed=set(CHANNEL_RIGHTS if kind=='channel' else GROUP_RIGHTS)
        if kind=='group' and not getattr(entity,'forum',False):
            allowed.discard('manage_topics')
        bad=[right for right in rights if right not in allowed]
        if bad:
            raise BackendError('invalid_input','These rights do not apply to this '+('channel' if kind=='channel' else 'group')+': '+', '.join(bad)+'. Applicable: '+', '.join(sorted(allowed))+'.')
        return list(dict.fromkeys(rights))

    async def _bot_user(self,bot):
        user=await self._bot_entity(bot)
        if not getattr(user,'bot',False):
            raise BackendError('out_of_scope','Changing the rights of people is outside this version; only bots can be changed.')
        self._known_users[user.id]=user
        return user

    async def _participant(self,entity,user):
        f,t,u=_tl()
        client=self._client
        try:
            found=await client(f.channels.GetParticipantRequest(channel=u.get_input_channel(await client.get_input_entity(entity)),participant=await client.get_input_entity(user)))
        except Exception as error:
            if type(error).__name__ in ('UserNotParticipantError','ParticipantIdInvalidError'):
                return {'member':False,'is_admin':False,'is_creator':False,'rights':_blank(),'rank':None}
            raise
        participant=found.participant
        kind=type(participant).__name__
        granted=getattr(participant,'admin_rights',None)
        return {'member':kind not in ('ChannelParticipantLeft','ChannelParticipantBanned'),'is_admin':kind in ('ChannelParticipantAdmin','ChannelParticipantCreator'),
                'is_creator':kind=='ChannelParticipantCreator','rights':{right:bool(getattr(granted,right,False)) for right in ALL_FLAGS},'rank':getattr(participant,'rank',None)}

    @_operation
    async def list_admins(self,chat_id,limit=100):
        f,t,u=_tl()
        entity,chat=await self._resolve(chat_id)
        self._chat_kind(entity)
        admins=[]
        async for user in self._client.iter_participants(entity,filter=t.ChannelParticipantsAdmins(),limit=_limit(limit,200)):
            participant=getattr(user,'participant',None)
            granted=getattr(participant,'admin_rights',None)
            admins.append({**_user(user),'is_creator':type(participant).__name__=='ChannelParticipantCreator','rank':getattr(participant,'rank',None),
                           'rights':{right:bool(getattr(granted,right,False)) for right in ALL_FLAGS}})
            self._known_users[user.id]=user
        return {'chat':chat,'admins':admins,'bots':[admin['telegram_id'] for admin in admins if admin['bot']],'data_trust':UNTRUSTED}

    def _admin_draft(self,operation,chat,bot_user,before,after,rank_before,rank_after,remove=False):
        change={right:{'was':before[right],'now':after[right],'changed':before[right]!=after[right]} for right in ALL_FLAGS}
        preview={'action':'bot_admin','operation':operation,'chat':chat,'bot':{**_user(bot_user)},'rights':change,
                 'changed_rights':[right for right,item in change.items() if item['changed']],
                 'title':{'was':rank_before,'now':rank_after},'was_admin':before is not None and any(before.values()),
                 'destructive':remove or operation in ('demote','edit') and any(item['was'] and not item['now'] for item in change.values()),
                 'requires_manage_scope':True,'data_trust':UNTRUSTED}
        if remove:
            preview['effect']='The bot is removed from the chat.'
        payload={'action':'bot_admin','operation':operation,'chat_id':chat['chat_id'],'user_id':bot_user.id,'bot_username':getattr(bot_user,'username',None),
                 'rights':[right for right in RIGHTS if after[right]],'keep_add_admins':bool(after['add_admins']),'rank':rank_after or '','random_id':secrets.randbits(31)}
        return self.actions.prepare(payload,preview)

    async def _admin_context(self,chat_id,bot):
        entity,chat=await self._resolve(chat_id)
        kind=self._chat_kind(entity)
        user=await self._bot_user(bot)
        current=await self._participant(entity,user)
        return entity,chat,kind,user,current

    @_operation
    async def prepare_add_bot_admin(self,chat_id,bot,rights,title=None):
        entity,chat,kind,user,current=await self._admin_context(chat_id,bot)
        self._check_title(kind,title)
        rights=self._check_rights(kind,entity,rights)
        if not rights:
            raise BackendError('invalid_input','Choose at least one right; an administrator without rights is a plain member.')
        if current['is_admin']:
            raise BackendError('already_admin','The bot is already an administrator here. Use telegram_prepare_edit_bot_rights to change its rights.',diagnostics={'current_rights':[right for right,on in current['rights'].items() if on]})
        after={**_blank(),**{right:True for right in rights}}
        rank=_text(title,16,'title',True) if title else None
        return self._admin_draft('add',chat,user,current['rights'],after,current['rank'],rank)

    @_operation
    async def prepare_edit_bot_rights(self,chat_id,bot,grant=None,revoke=None,title=None):
        entity,chat,kind,user,current=await self._admin_context(chat_id,bot)
        self._check_title(kind,title)
        grant=self._check_rights(kind,entity,grant or [])
        revoke=self._check_rights(kind,entity,revoke or [])
        if set(grant)&set(revoke):
            raise BackendError('invalid_input','A right cannot be both granted and revoked.')
        if not current['is_admin']:
            raise BackendError('not_admin','The bot is not an administrator here. Use telegram_prepare_add_bot_admin.')
        if current['is_creator']:
            raise BackendError('out_of_scope','The creator of the chat cannot be changed.')
        after={**current['rights'],**{right:True for right in grant},**{right:False for right in revoke}}
        rank=_text(title,16,'title',True) if title is not None else current['rank']
        if after==current['rights'] and rank==current['rank']:
            raise BackendError('no_change','The bot already has exactly these rights and title.')
        return self._admin_draft('edit',chat,user,current['rights'],after,current['rank'],rank)

    @_operation
    async def prepare_demote_bot(self,chat_id,bot):
        entity,chat,kind,user,current=await self._admin_context(chat_id,bot)
        if not current['is_admin']:
            raise BackendError('not_admin','The bot is not an administrator here.')
        if current['is_creator']:
            raise BackendError('out_of_scope','The creator of the chat cannot be changed.')
        return self._admin_draft('demote',chat,user,current['rights'],_blank(),current['rank'],None)

    @_operation
    async def prepare_remove_bot(self,chat_id,bot):
        entity,chat,kind,user,current=await self._admin_context(chat_id,bot)
        if not current['member']:
            raise BackendError('not_a_member','The bot is not a member of this chat.')
        if current['is_creator']:
            raise BackendError('out_of_scope','The creator of the chat cannot be removed.')
        return self._admin_draft('remove',chat,user,current['rights'],_blank(),current['rank'],None,remove=True)

    @_operation
    async def prepare_set_bot_title(self,chat_id,bot,title):
        entity,chat,kind,user,current=await self._admin_context(chat_id,bot)
        self._check_title(kind,title)
        if not current['is_admin']:
            raise BackendError('not_admin','Only administrators have a custom title; add the bot as administrator first.')
        rank=_text(title,16,'title',True) or None
        if rank==current['rank']:
            raise BackendError('no_change','The bot already has this title.')
        return self._admin_draft('title',chat,user,current['rights'],current['rights'],current['rank'],rank)

    async def _bot_admin(self,payload):
        f,t,u=_tl()
        client=self._client
        entity,chat=await self._resolve(payload['chat_id'])
        kind=self._chat_kind(entity)
        if payload['user_id'] in self._known_users:
            user_peer=await client.get_input_entity(self._known_users[payload['user_id']])
        else:
            user_peer=await client.get_input_entity('@'+payload['bot_username']) if payload.get('bot_username') else await client.get_input_entity(payload['user_id'])
        # Rights already held (possibly set by hand) are carried over; only add_admins and unknown names are refused.
        checked=self._check_rights(kind,entity,payload['rights'],applicable=False)
        operation=payload['operation']
        if operation=='remove':
            await client.kick_participant(entity,user_peer)
            return {'changed':True,'operation':'remove','chat':chat,'bot_id':payload['user_id']}
        rights=t.ChatAdminRights(**{right:right in checked for right in RIGHTS},add_admins=bool(payload.get('keep_add_admins')))
        await client(f.channels.EditAdminRequest(channel=u.get_input_channel(await client.get_input_entity(entity)),user_id=u.get_input_user(user_peer),admin_rights=rights,rank=payload['rank'] or ''))
        return {'changed':True,'operation':operation,'chat':chat,'bot_id':payload['user_id'],'rights_now':checked,'title_now':payload['rank'] or None}
