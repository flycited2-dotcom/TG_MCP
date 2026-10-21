"""Finding chats: the account's own chat list, public search, and a careful look at one chat before joining or posting."""
from __future__ import annotations
import re

from telegram_backend import BackendError,_operation,_limit,_text
from bot_interaction import UNTRUSTED

NAME=re.compile(r'^@?([A-Za-z][A-Za-z0-9_]{3,31})$')
HASH=re.compile(r'^[A-Za-z0-9_-]{5,64}$')
RESERVED={'c','s','addlist','addemoji','addstickers','share','proxy','socks','login','iv','boost','m','giftcode'}
NO_ADS=re.compile(r'(без\s+реклам|реклам\w*\s+(?:запрещ|не\s+допуска|не\s+разреш|платн)|запрещ\w+\s+реклам|не\s+реклам|реклам\w*\s+только\s+(?:через|по\s+согласован|у\s+админ)|no\s+ads|no\s+advert|advertis\w+\s+(?:is\s+)?(?:forbidden|prohibited|not\s+allowed)|спам\s+(?:=|-|—)?\s*бан)',re.I)
ADS_OK=re.compile(r'(реклам\w*\s+(?:разрешен|по\s+вторник|по\s+суббот|в\s+понедельник|раз\s+в)|можно\s+размещ|размещени\w+\s+объявлен|ads?\s+(?:allowed|welcome))',re.I)


def _tl():
    from telethon.tl import functions,types
    from telethon import utils
    return functions,types,utils


def parse_target(value):
    """Accept @name, t.me/name, t.me/+hash, t.me/joinchat/hash, tg://join?invite=hash; never a bare number."""
    if not isinstance(value,str) or not value.strip() or len(value)>300:
        raise BackendError('invalid_input','Pass a @username, a t.me link or an invite link.')
    text=value.strip()
    found=re.match(r'^tg://join\?invite=([A-Za-z0-9_-]{5,64})$',text,re.I)
    if found:
        return {'kind':'invite','hash':found.group(1)}
    found=re.match(r'^tg://resolve\?domain=([A-Za-z][A-Za-z0-9_]{3,31})',text,re.I)
    if found:
        return {'kind':'username','name':found.group(1)}
    found=re.match(r'^(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(.+)$',text,re.I)
    if found:
        path=found.group(1).split('?')[0].split('#')[0].strip('/')
        if path.startswith('+'):
            token=path[1:]
        elif path.lower().startswith('joinchat/'):
            token=path[9:]
        else:
            name=path.split('/')[0]
            if name.lower() in RESERVED or not NAME.match(name):
                raise BackendError('invalid_input','This link does not point to a chat.')
            return {'kind':'username','name':name}
        if not HASH.match(token):
            raise BackendError('invalid_input','The invite link looks invalid.')
        return {'kind':'invite','hash':token}
    found=NAME.match(text)
    if found:
        return {'kind':'username','name':found.group(1)}
    raise BackendError('invalid_input','Pass a @username, a t.me link or an invite link.')


def chat_type(entity):
    name=type(entity).__name__
    if name=='User':
        return 'bot' if getattr(entity,'bot',False) else 'private'
    if name=='Channel':
        return 'channel' if getattr(entity,'broadcast',False) else 'supergroup'
    return 'group'


def ads_hint(*texts):
    blob=' '.join(t for t in texts if t)
    if not blob:
        return None
    if NO_ADS.search(blob):
        return 'rules_may_forbid_ads'
    return 'rules_mention_ads' if ADS_OK.search(blob) else None


class ChatDiscovery:
    """Mixed into ExtendedBackend."""

    def _row(self,entity,dialog=None):
        _,_,u=_tl()
        try:
            chat_id=u.get_peer_id(entity)
        except Exception:
            chat_id=getattr(entity,'id',None)
        row={'chat_id':chat_id,'title':getattr(entity,'title',None) or ' '.join(filter(None,[getattr(entity,'first_name',None),getattr(entity,'last_name',None)])),
             'username':getattr(entity,'username',None),'type':chat_type(entity)}
        count=getattr(entity,'participants_count',None)
        if count is not None:
            row['members']=count
        if dialog is not None:
            row['archived']=bool(getattr(dialog,'archived',False))
            row['pinned']=bool(getattr(dialog,'pinned',False))
        for flag in ('verified','scam','fake','join_request','join_to_send'):
            if getattr(entity,flag,False):
                row[flag]=True
        return row

    async def _dialog_map(self):
        found={}
        async for dialog in self._client.iter_dialogs():
            found[dialog.id]=dialog
        return found

    @_operation
    async def list_chats(self,query=None,kind=None,folder=None,limit=100):
        """The account's own chats (not a search of Telegram). Filter by name, kind or folder."""
        limit=_limit(limit,300)
        if kind not in (None,'group','supergroup','channel','private','bot','groups_and_channels'):
            raise BackendError('invalid_input','kind must be group, supergroup, channel, private, bot or groups_and_channels.')
        wanted=_text(query,200,'query').strip().casefold() if query else None
        members=None
        if folder is not None:
            members=await self._folder_chat_ids(folder)
        await self._ready()
        rows=[]
        async for dialog in self._client.iter_dialogs():
            entity=dialog.entity
            row=self._row(entity,dialog)
            if kind=='groups_and_channels' and row['type'] in ('private','bot'):
                continue
            if kind not in (None,'groups_and_channels') and row['type']!=kind:
                continue
            if wanted and wanted not in (row['title'] or '').casefold() and wanted not in (row['username'] or '').casefold():
                continue
            if members is not None and dialog.id not in members:
                continue
            row['chat_id']=dialog.id
            rows.append(row)
            if len(rows)>=limit:
                break
        return {'chats':rows,'count':len(rows),'folder':folder,'data_trust':UNTRUSTED}

    @_operation
    async def search_public_chats(self,query,limit=20,kind=None):
        """Search public groups and channels of all Telegram by name or username (what the app's global search finds)."""
        f,t,u=_tl()
        query=_text(query,200,'query').strip()
        limit=_limit(limit,50)
        if kind not in (None,'group','channel'):
            raise BackendError('invalid_input','kind must be group or channel.')
        client=await self._ready()
        found=await client(f.contacts.SearchRequest(q=query,limit=limit))
        rows=[]
        for entity in getattr(found,'chats',[]) or []:
            if type(entity).__name__ not in ('Channel','Chat'):
                continue
            row=self._row(entity)
            if kind=='group' and row['type']=='channel' or kind=='channel' and row['type']!='channel':
                continue
            row['member']=not bool(getattr(entity,'left',True))
            if row.get('username'):
                row['target']='@'+row['username']
                self._known_users[('chat',row['username'].casefold())]=entity
            rows.append(row)
        return {'query':query,'results':rows,'count':len(rows),
                'hint':'Search matches names and usernames only. Inspect a promising result with telegram_inspect_chat before joining or posting.','data_trust':UNTRUSTED}

    @_operation
    async def inspect_chat(self,target):
        """Look at a chat without joining it: size, rules text, slow mode, approval requirement, posting rights."""
        f,t,u=_tl()
        client=await self._ready()
        parsed=parse_target(target)
        if parsed['kind']=='invite':
            try:
                info=await client(f.messages.CheckChatInviteRequest(parsed['hash']))
            except Exception as error:
                if type(error).__name__ in ('InviteHashExpiredError','InviteHashInvalidError','InviteHashEmptyError'):
                    raise BackendError('invite_invalid','This invite link is invalid or expired.') from None
                raise
            if type(info).__name__=='ChatInviteAlready':
                entity=info.chat
                return {**self._row(entity),'member':True,'target':target,'note':'You are already a member.','data_trust':UNTRUSTED}
            about=getattr(info,'about',None)
            warnings=[]
            if getattr(info,'request_needed',False):
                warnings.append('join_request_required')
            for flag in ('scam','fake'):
                if getattr(info,flag,False):
                    warnings.append(flag+'_flag')
            if ads_hint(about)=='rules_may_forbid_ads':
                warnings.append('rules_may_forbid_ads')
            return {'target':target,'title':getattr(info,'title',None),'type':'channel' if getattr(info,'broadcast',False) else 'supergroup' if getattr(info,'megagroup',False) else 'group',
                    'members':getattr(info,'participants_count',None),'about':about,'requires_approval':bool(getattr(info,'request_needed',False)),'member':False,
                    'public':bool(getattr(info,'public',False)),'warnings':warnings,'data_trust':UNTRUSTED}
        try:
            entity=await client.get_entity('@'+parsed['name'])
        except Exception as error:
            if type(error).__name__ in ('UsernameNotOccupiedError','UsernameInvalidError') or isinstance(error,ValueError):
                raise BackendError('chat_not_found','No public chat with this username.') from None
            raise
        if type(entity).__name__=='User':
            raise BackendError('not_a_chat','This username belongs to a person or bot, not a group or channel.')
        row=self._row(entity)
        row['member']=not bool(getattr(entity,'left',True))
        warnings=[]
        about,pinned_text,slowmode,linked=None,None,None,None
        if type(entity).__name__=='Channel':
            full=await client(f.channels.GetFullChannelRequest(channel=u.get_input_channel(await client.get_input_entity(entity))))
            info=full.full_chat
            about=getattr(info,'about',None)
            slowmode=getattr(info,'slowmode_seconds',None)
            linked=getattr(info,'linked_chat_id',None)
            row['members']=getattr(info,'participants_count',row.get('members'))
            pinned=getattr(info,'pinned_msg_id',None)
            if pinned:
                try:
                    message=await client.get_messages(entity,ids=pinned)
                    pinned_text=(getattr(message,'message',None) or '')[:1500] or None
                except Exception:
                    pinned_text=None
        default=getattr(entity,'default_banned_rights',None)
        if getattr(entity,'broadcast',False):
            warnings.append('channel_posting_needs_admin_rights')
        elif default is not None and getattr(default,'send_messages',False):
            warnings.append('members_cannot_post')
        if getattr(entity,'join_request',False):
            warnings.append('join_request_required')
        if getattr(entity,'join_to_send',False):
            warnings.append('must_join_to_post')
        if slowmode:
            warnings.append(f'slow_mode_{slowmode}s')
        for flag in ('scam','fake'):
            if getattr(entity,flag,False):
                warnings.append(flag+'_flag')
        if ads_hint(about,pinned_text)=='rules_may_forbid_ads':
            warnings.append('rules_may_forbid_ads')
        row.update({'target':'@'+parsed['name'],'about':about,'pinned_message_text':pinned_text,'slow_mode_seconds':slowmode,'has_linked_chat':bool(linked),
                    'requires_approval':bool(getattr(entity,'join_request',False)),'warnings':warnings,
                    'rules_notice':'Read about and pinned_message_text yourself: automatic hints are keyword matches, not a decision. Never post advertising where the rules forbid it.',
                    'data_trust':UNTRUSTED})
        self._known_users[('chat',parsed['name'].casefold())]=entity
        return row
