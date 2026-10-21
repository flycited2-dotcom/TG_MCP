"""The remaining everyday Telegram functions, all reviewed (prepare -> preview -> execute) and none touching
account security, privacy settings, sessions, payments or bulk personal data:

chat list settings, deleting dialogs, searching all messages, scheduled messages, polls, settings of groups the owner
administers, invite links, join requests, forum topics, the admin log, deleting an owned chat and blocking a user.
"""
from __future__ import annotations
import re
import secrets
import time

from telegram_backend import BackendError,_operation,_limit,_text,_safe_error
from bot_interaction import UNTRUSTED
from chat_discovery import chat_type
from jobs import STOP_CODES
from botfather import guard_recipient

SETTINGS={'mute':'Notifications are muted (for the given hours, or forever).','unmute':'Notifications are turned back on.','archive':'The chat moves to the archive.',
          'unarchive':'The chat returns from the archive.','pin_chat':'The chat is pinned at the top of your chat list (Telegram allows only a few).',
          'unpin_chat':'The chat is unpinned from the top of the list.','mark_read':'All messages in the chat are marked as read.'}
PERMISSIONS=('send_messages','send_media','send_stickers','send_gifs','send_polls','embed_links','invite_users','pin_messages','change_info')
SLOW_MODE=(0,10,30,60,300,900,3600)
ADMIN_SETTINGS=('username','slow_mode','member_permissions','photo','join_approval','join_to_send')
TOPIC_OPERATIONS=('rename','close','reopen','delete')
PAUSE=0.4


def _tl():
    from telethon.tl import functions,types
    from telethon import utils
    return functions,types,utils


def _ids(values,maximum,field='chat_ids'):
    if not isinstance(values,list) or not 1<=len(values)<=maximum:
        raise BackendError('invalid_input',f'{field} must list 1..{maximum} numeric IDs.')
    for value in values:
        if isinstance(value,bool) or not isinstance(value,int):
            raise BackendError('invalid_input',f'{field} must be numeric IDs.')
    return list(dict.fromkeys(values))


class ChatTools:
    """Mixed into ExtendedBackend (needs ChatDiscovery for _row/_dialog_map)."""

    CHAT_ACTIONS={'chat_setting':'_run_chat_setting','delete_dialogs':'_run_delete_dialogs','chat_admin_setting':'_run_admin_setting','invite_link':'_run_invite_link',
                  'join_requests':'_run_join_requests','topic_change':'_run_topic_change','delete_chat_for_all':'_run_delete_for_all','block_user':'_run_block_user',
                  'delete_scheduled':'_run_delete_scheduled'}

    async def _each(self,items,operation):
        """Run one short Telegram call per item; a flood or unknown failure stops the rest (never a blind retry)."""
        results,stopped=[],False
        for index,item in enumerate(items):
            if stopped:
                results.append({**item['row'],'status':'not_attempted'})
                continue
            if index:
                await self._sleep(PAUSE)
            try:
                outcome=await operation(item)
                results.append({**item['row'],'status':'done',**(outcome or {})})
            except Exception as error:
                failure=error if isinstance(error,BackendError) else _safe_error(error)
                results.append({**item['row'],'status':'failed','error':failure.code,'message':str(failure)[:160]})
                stopped=failure.code in STOP_CODES
        done=sum(1 for result in results if result['status']=='done')
        return {'changed':done>0,'done':done,'results':results,'stopped':stopped}

    @staticmethod
    def _admin_of(entity,right=None):
        if getattr(entity,'creator',False):
            return True
        rights=getattr(entity,'admin_rights',None)
        return rights is not None and (right is None or bool(getattr(rights,right,False)))

    def _need_admin(self,entity,right=None,what='this'):
        if not self._admin_of(entity,right):
            raise BackendError('permission_denied',f'You need to be an administrator{" with the "+right+" right" if right else ""} to change {what}.')

    # ---- chat list settings -----------------------------------------------------------------------------------

    @_operation
    async def prepare_chat_setting(self,chat_ids,setting,hours=None):
        if setting not in SETTINGS:
            raise BackendError('invalid_input','setting must be one of: '+', '.join(SETTINGS)+'.')
        if hours is not None and (setting!='mute' or isinstance(hours,bool) or not isinstance(hours,int) or not 1<=hours<=8760):
            raise BackendError('invalid_input','hours (1..8760) applies only to mute; leave it empty to mute forever.')
        ids=_ids(chat_ids,50)
        dialogs=await self._dialog_map()
        rows,found=[],[]
        for chat_id in ids:
            entry=dialogs.get(chat_id)
            if entry is None:
                rows.append({'chat_id':chat_id,'status':'skip','reason':'not_in_your_chat_list'})
                continue
            row={**self._row(entry.entity,entry),'chat_id':chat_id,'status':'will_apply'}
            if setting=='archive' and row.get('archived') or setting=='unarchive' and not row.get('archived'):
                row.update({'status':'skip','reason':'already_so'})
            elif setting=='pin_chat' and row.get('pinned') or setting=='unpin_chat' and not row.get('pinned'):
                row.update({'status':'skip','reason':'already_so'})
            else:
                found.append(chat_id)
            rows.append(row)
        if not found:
            raise BackendError('nothing_to_change','Nothing to change for these chats.',diagnostics={'chats':rows})
        preview={'action':'chat_setting','setting':setting,'hours':hours,'chats':rows,'will_apply':len(found),'effect':SETTINGS[setting],'reversible':True,'requires_manage_scope':True,'data_trust':UNTRUSTED}
        return self.actions.prepare({'action':'chat_setting','setting':setting,'hours':hours,'chat_ids':found},preview)

    async def _run_chat_setting(self,p):
        f,t,u=_tl()
        client=self._client
        setting=p['setting']

        async def one(item):
            entity,chat=await self._resolve(item['chat_id'])
            peer=await client.get_input_entity(entity)
            if setting in ('mute','unmute'):
                until=0 if setting=='unmute' else (int(self._clock()+p['hours']*3600) if p['hours'] else 2147483647)
                await client(f.account.UpdateNotifySettingsRequest(peer=t.InputNotifyPeer(peer=peer),settings=t.InputPeerNotifySettings(mute_until=until)))
            elif setting in ('archive','unarchive'):
                await client(f.folders.EditPeerFoldersRequest(folder_peers=[t.InputFolderPeer(peer=peer,folder_id=1 if setting=='archive' else 0)]))
            elif setting in ('pin_chat','unpin_chat'):
                try:
                    await client(f.messages.ToggleDialogPinRequest(peer=t.InputDialogPeer(peer=peer),pinned=setting=='pin_chat'))
                except Exception as error:
                    if 'PinnedDialogsTooMuch' in type(error).__name__ or 'PINNED_DIALOGS_TOO_MUCH' in str(getattr(error,'message','')):
                        raise BackendError('pin_limit','Telegram allows only a few pinned chats; unpin one first.') from None
                    raise
            else:
                if type(entity).__name__=='Channel':
                    await client(f.channels.ReadHistoryRequest(channel=u.get_input_channel(peer),max_id=0))
                else:
                    await client(f.messages.ReadHistoryRequest(peer=peer,max_id=0))
            return None
        return await self._each([{'chat_id':chat_id,'row':{'chat_id':chat_id}} for chat_id in p['chat_ids']],one)

    # ---- deleting dialogs -------------------------------------------------------------------------------------

    @_operation
    async def prepare_delete_chats(self,chat_ids,for_everyone=False):
        ids=_ids(chat_ids,30)
        dialogs=await self._dialog_map()
        rows,found=[],[]
        for chat_id in ids:
            entry=dialogs.get(chat_id)
            if entry is None:
                rows.append({'chat_id':chat_id,'status':'skip','reason':'not_in_your_chat_list'})
                continue
            row={**self._row(entry.entity,entry),'chat_id':chat_id}
            if row['type'] not in ('private','bot'):
                row.update({'status':'skip','reason':'groups_and_channels_use_leave_chats'})
            else:
                row['status']='will_delete'
                found.append(chat_id)
            rows.append(row)
        if not found:
            raise BackendError('nothing_to_delete','None of these is a private chat or bot dialog.',diagnostics={'chats':rows})
        preview={'action':'delete_dialogs','chats':rows,'will_delete':len(found),'for_everyone':bool(for_everyone),'destructive':True,
                 'effect':'The conversation history is deleted'+(' for you AND for the other person (where Telegram allows it)' if for_everyone else ' from your account only; the other person keeps theirs')+'.',
                 'requires_manage_scope':True,'data_trust':UNTRUSTED}
        return self.actions.prepare({'action':'delete_dialogs','chat_ids':found,'for_everyone':bool(for_everyone)},preview)

    async def _run_delete_dialogs(self,p):
        f,t,u=_tl()

        async def one(item):
            entity,chat=await self._resolve(item['chat_id'])
            peer=await self._client.get_input_entity(entity)
            for _ in range(20):
                affected=await self._client(f.messages.DeleteHistoryRequest(peer=peer,max_id=0,just_clear=False,revoke=p['for_everyone']))
                if not getattr(affected,'offset',0):
                    break
            return {'history_deleted':True}
        return await self._each([{'chat_id':chat_id,'row':{'chat_id':chat_id}} for chat_id in p['chat_ids']],one)

    # ---- searching and scheduled messages ---------------------------------------------------------------------

    @_operation
    async def search_all_messages(self,query,limit=30):
        f,t,u=_tl()
        client=await self._ready()
        text=_text(query,200,'query').strip()
        limit=_limit(limit,50)
        found=await client(f.messages.SearchGlobalRequest(q=text,filter=t.InputMessagesFilterEmpty(),min_date=None,max_date=None,offset_rate=0,offset_peer=t.InputPeerEmpty(),offset_id=0,limit=limit))
        names={}
        for chat in getattr(found,'chats',[]) or []:
            names[u.get_peer_id(chat)]=getattr(chat,'title',None)
        for user in getattr(found,'users',[]) or []:
            names[user.id]=' '.join(filter(None,[getattr(user,'first_name',None),getattr(user,'last_name',None)])) or getattr(user,'username',None)
        rows=[]
        for message in getattr(found,'messages',[]) or []:
            peer=getattr(message,'peer_id',None)
            if peer is None:
                continue
            chat_id=u.get_peer_id(peer)
            date=getattr(message,'date',None)
            rows.append({'chat_id':chat_id,'chat_title':names.get(chat_id),'message_id':message.id,'date':date.isoformat() if date else None,
                         'text':(getattr(message,'message','') or '')[:300],'outgoing':bool(getattr(message,'out',False))})
        return {'query':text,'results':rows,'count':len(rows),'scope':'Messages of your own chats only.','data_trust':UNTRUSTED}

    @_operation
    async def get_scheduled(self,chat_id):
        f,t,u=_tl()
        entity,chat=await self._resolve(chat_id)
        found=await self._client(f.messages.GetScheduledHistoryRequest(peer=await self._client.get_input_entity(entity),hash=0))
        rows=[{'message_id':m.id,'scheduled_for':m.date.isoformat() if getattr(m,'date',None) else None,'text':(getattr(m,'message','') or '')[:500],'has_media':bool(getattr(m,'media',None))}
              for m in getattr(found,'messages',[]) or []]
        return {'chat':chat,'scheduled':rows,'count':len(rows),'data_trust':UNTRUSTED}

    @_operation
    async def prepare_delete_scheduled(self,chat_id,message_ids):
        f,t,u=_tl()
        ids=_ids(message_ids,100,'message_ids')
        entity,chat=await self._resolve(chat_id)
        found=await self._client(f.messages.GetScheduledHistoryRequest(peer=await self._client.get_input_entity(entity),hash=0))
        have={m.id:m for m in getattr(found,'messages',[]) or []}
        missing=[i for i in ids if i not in have]
        if missing:
            raise BackendError('message_not_found','These are not scheduled messages of this chat: '+', '.join(map(str,missing))+'.')
        shown=[{'message_id':i,'scheduled_for':have[i].date.isoformat() if getattr(have[i],'date',None) else None,'text':getattr(have[i],'message','') or ''} for i in ids]
        preview={'action':'delete_scheduled','chat':chat,'messages':shown,'destructive':True,'data_trust':UNTRUSTED}
        return self.actions.prepare({'action':'delete_scheduled','chat_id':chat['chat_id'],'message_ids':ids},preview)

    async def _run_delete_scheduled(self,p):
        f,t,u=_tl()
        entity,chat=await self._resolve(p['chat_id'])
        await self._client(f.messages.DeleteScheduledMessagesRequest(peer=await self._client.get_input_entity(entity),id=p['message_ids']))
        return {'deleted':True,'chat':chat,'message_ids':p['message_ids']}

    # ---- polls ------------------------------------------------------------------------------------------------

    @_operation
    async def prepare_poll(self,chat_id,question,options,multiple=False,anonymous=True,quiz_answer=None):
        question=_text(question,255,'question').strip()
        if not isinstance(options,list) or not 2<=len(options)<=10:
            raise BackendError('invalid_input','A poll needs 2..10 options.')
        options=[_text(option,100,'option').strip() for option in options]
        if len(set(option.casefold() for option in options))!=len(options):
            raise BackendError('invalid_input','Poll options must be different.')
        if quiz_answer is not None:
            if isinstance(quiz_answer,bool) or not isinstance(quiz_answer,int) or not 0<=quiz_answer<len(options) or multiple:
                raise BackendError('invalid_input','A quiz needs quiz_answer as the 0-based index of the right option and cannot allow several answers.')
        entity,chat=await self._target(chat_id)
        guard_recipient(chat)
        preview={'action':'send_poll','recipient':chat,'question':question,'choices':options,'multiple_answers':bool(multiple),'anonymous':bool(anonymous),
                 'quiz_correct_option':options[quiz_answer] if quiz_answer is not None else None}
        payload={'action':'send_poll','chat_id':chat['chat_id'],'question':question,'choices':options,'multiple_answers':bool(multiple),'anonymous':bool(anonymous),
                 'quiz_answer':quiz_answer,'random_id':secrets.randbits(62)+1,'poll_id':secrets.randbits(62)+1}
        return self.actions.prepare(payload,preview)

    def _poll_media(self,p):
        _,t,_=_tl()
        answers=[t.PollAnswer(text=t.TextWithEntities(text=option,entities=[]),option=bytes([index])) for index,option in enumerate(p['choices'])]
        poll=t.Poll(id=p['poll_id'],question=t.TextWithEntities(text=p['question'],entities=[]),answers=answers,hash=0,public_voters=not p['anonymous'],
                    multiple_choice=bool(p['multiple_answers']),quiz=p.get('quiz_answer') is not None)
        return t.InputMediaPoll(poll=poll,correct_answers=[p['quiz_answer']] if p.get('quiz_answer') is not None else None)

    # ---- settings of chats the owner administers ---------------------------------------------------------------

    @_operation
    async def prepare_chat_admin_setting(self,chat_id,setting,value=None,file_id=None,permissions=None):
        f,t,u=_tl()
        if setting not in ADMIN_SETTINGS:
            raise BackendError('invalid_input','setting must be one of: '+', '.join(ADMIN_SETTINGS)+'.')
        entity,chat=await self._resolve(chat_id)
        kind=chat_type(entity)
        if kind not in ('group','supergroup','channel'):
            raise BackendError('invalid_input','Select a group or channel.')
        is_channel=type(entity).__name__=='Channel'
        payload={'action':'chat_admin_setting','chat_id':chat['chat_id'],'setting':setting}
        preview={'action':'chat_admin_setting','chat':chat,'setting':setting,'requires_manage_scope':True}
        if setting=='username':
            if not is_channel:
                raise BackendError('unsupported_operation','Only channels and supergroups can have a public username.')
            self._need_admin(entity,None,'the public address')
            name=(value or '').strip().lstrip('@')
            if name and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',name):
                raise BackendError('invalid_input','A public username has 5..32 letters, digits or _ and starts with a letter. Pass an empty value to make the chat private.')
            if name and name!=(getattr(entity,'username',None) or ''):
                free=await self._client(f.channels.CheckUsernameRequest(channel=u.get_input_channel(await self._client.get_input_entity(entity)),username=name))
                if not free:
                    raise BackendError('username_taken','This public address is taken or not allowed.')
            if name==(getattr(entity,'username',None) or ''):
                raise BackendError('no_change','The chat already has this address.')
            preview.update({'was':getattr(entity,'username',None),'now':name or None,'note':'A public address lets anyone find and join the chat.' if name else 'The chat becomes private (link-only).'})
            payload['value']=name
        elif setting=='slow_mode':
            if not (is_channel and getattr(entity,'megagroup',False)):
                raise BackendError('unsupported_operation','Slow mode exists only in supergroups.')
            self._need_admin(entity,None,'slow mode')
            if value not in SLOW_MODE:
                raise BackendError('invalid_input','Slow mode seconds must be one of: '+', '.join(map(str,SLOW_MODE))+' (0 turns it off).')
            preview.update({'now_seconds':value})
            payload['value']=value
        elif setting=='member_permissions':
            self._need_admin(entity,'ban_users','member permissions')
            if not isinstance(permissions,dict) or not permissions or any(key not in PERMISSIONS or not isinstance(flag,bool) for key,flag in permissions.items()):
                raise BackendError('invalid_input','permissions must map some of '+', '.join(PERMISSIONS)+' to true (members may) or false (members may not).')
            current=getattr(entity,'default_banned_rights',None)
            table={key:{'was_allowed':not bool(getattr(current,key,False)),'now_allowed':permissions.get(key,not bool(getattr(current,key,False)))} for key in PERMISSIONS}
            changed=[key for key,item in table.items() if item['was_allowed']!=item['now_allowed']]
            if not changed:
                raise BackendError('no_change','The members already have exactly these permissions.')
            preview.update({'permissions':table,'changed':changed})
            payload['permissions']=permissions
        elif setting=='photo':
            self._need_admin(entity,'change_info','the photo')
            info=self.files.metadata(file_id or '')
            if info['mime_type'] not in ('image/jpeg','image/png'):
                raise BackendError('unsupported_media','A chat photo must be a JPEG or PNG image.')
            preview.update({'media':self._media_preview([{'file_id':file_id,'kind':'photo','caption':'','original':False}])})
            payload['file_id']=file_id
        else:
            if not is_channel:
                raise BackendError('unsupported_operation','This setting exists only in channels and supergroups.')
            if setting=='join_to_send' and not getattr(entity,'megagroup',False):
                raise BackendError('unsupported_operation','Joining to write applies only to supergroups.')
            self._need_admin(entity,'invite_users','who can join')
            if not isinstance(value,bool):
                raise BackendError('invalid_input','value must be true or false.')
            preview.update({'now':value,'meaning':'New members need an admin approval.' if setting=='join_approval' else 'Only members can write in the discussion.'})
            payload['value']=value
        return self.actions.prepare(payload,preview)

    async def _run_admin_setting(self,p):
        f,t,u=_tl()
        client=self._client
        entity,chat=await self._resolve(p['chat_id'])
        peer=await client.get_input_entity(entity)
        setting=p['setting']
        if setting=='username':
            await client(f.channels.UpdateUsernameRequest(channel=u.get_input_channel(peer),username=p['value']))
        elif setting=='slow_mode':
            await client(f.channels.ToggleSlowModeRequest(channel=u.get_input_channel(peer),seconds=p['value']))
        elif setting=='member_permissions':
            current=getattr(entity,'default_banned_rights',None)
            flags={name:bool(getattr(current,name,False)) for name in ('send_messages','send_media','send_stickers','send_gifs','send_polls','embed_links','invite_users','pin_messages','change_info')}
            for key,allowed in p['permissions'].items():
                flags[key]=not allowed
            await client(f.messages.EditChatDefaultBannedRightsRequest(peer=peer,banned_rights=t.ChatBannedRights(until_date=0,**flags)))
        elif setting=='photo':
            path=self.files.verified_path(p['file_id'])
            uploaded=await client.upload_file(path,file_name=self.files.metadata(p['file_id'])['filename'])
            photo=t.InputChatUploadedPhoto(file=uploaded)
            if type(entity).__name__=='Channel':
                await client(f.channels.EditPhotoRequest(channel=u.get_input_channel(peer),photo=photo))
            else:
                await client(f.messages.EditChatPhotoRequest(chat_id=entity.id,photo=photo))
        elif setting=='join_approval':
            await client(f.channels.ToggleJoinRequestRequest(channel=u.get_input_channel(peer),enabled=p['value']))
        else:
            await client(f.channels.ToggleJoinToSendRequest(channel=u.get_input_channel(peer),enabled=p['value']))
        return {'changed':True,'setting':setting,'chat':chat}

    # ---- invite links and join requests -----------------------------------------------------------------------

    @_operation
    async def list_invite_links(self,chat_id,revoked=False,limit=30):
        f,t,u=_tl()
        entity,chat=await self._resolve(chat_id)
        found=await self._client(f.messages.GetExportedChatInvitesRequest(peer=await self._client.get_input_entity(entity),admin_id=t.InputUserSelf(),limit=_limit(limit,100),revoked=bool(revoked) or None))
        rows=[]
        for item in getattr(found,'invites',[]) or []:
            expires=getattr(item,'expire_date',None)
            rows.append({'link':item.link,'title':getattr(item,'title',None),'joined':getattr(item,'usage',0),'usage_limit':getattr(item,'usage_limit',None),
                         'expires_at':expires.isoformat() if expires else None,'request_needed':bool(getattr(item,'request_needed',False)),'revoked':bool(getattr(item,'revoked',False)),
                         'permanent':bool(getattr(item,'permanent',False))})
        return {'chat':chat,'links':rows,'count':len(rows),'note':'Anyone holding a link can join; share links only with intended people.'}

    @_operation
    async def prepare_invite_link(self,chat_id,operation,title=None,expire_days=None,usage_limit=None,request_needed=False,link=None):
        entity,chat=await self._resolve(chat_id)
        self._need_admin(entity,'invite_users','invite links')
        if operation=='create':
            if request_needed and usage_limit:
                raise BackendError('invalid_input','Telegram does not allow a usage limit together with join approval.')
            if expire_days is not None and (isinstance(expire_days,bool) or not isinstance(expire_days,int) or not 1<=expire_days<=365):
                raise BackendError('invalid_input','expire_days must be 1..365.')
            if usage_limit is not None and (isinstance(usage_limit,bool) or not isinstance(usage_limit,int) or not 1<=usage_limit<=99999):
                raise BackendError('invalid_input','usage_limit must be 1..99999.')
            preview={'action':'invite_link','operation':'create','chat':chat,'title':_text(title,32,'title',True) if title else None,'expires_in_days':expire_days,'usage_limit':usage_limit,
                     'request_needed':bool(request_needed),'warning':'Anyone who gets the link can join (or ask to join).','requires_manage_scope':True}
            payload={'action':'invite_link','operation':'create','chat_id':chat['chat_id'],'title':preview['title'],'expire_days':expire_days,'usage_limit':usage_limit,'request_needed':bool(request_needed)}
        elif operation=='revoke':
            if not isinstance(link,str) or not re.match(r'^(https?://)?t\.me/(\+|joinchat/)[A-Za-z0-9_-]{5,}$',link.strip()):
                raise BackendError('invalid_input','Pass the invite link to revoke (a t.me/+… link).')
            preview={'action':'invite_link','operation':'revoke','chat':chat,'link':link.strip(),'destructive':True,'effect':'The link stops working; people already in the chat stay.','requires_manage_scope':True}
            payload={'action':'invite_link','operation':'revoke','chat_id':chat['chat_id'],'link':link.strip()}
        else:
            raise BackendError('invalid_input','operation must be create or revoke.')
        return self.actions.prepare(payload,preview)

    async def _run_invite_link(self,p):
        f,t,u=_tl()
        from datetime import datetime,timedelta,timezone
        entity,chat=await self._resolve(p['chat_id'])
        peer=await self._client.get_input_entity(entity)
        if p['operation']=='create':
            expires=datetime.now(timezone.utc)+timedelta(days=p['expire_days']) if p['expire_days'] else None
            result=await self._client(f.messages.ExportChatInviteRequest(peer=peer,request_needed=p['request_needed'] or None,expire_date=expires,usage_limit=p['usage_limit'],title=p['title']))
            return {'created':True,'chat':chat,'link':getattr(result,'link',None)}
        await self._client(f.messages.EditExportedChatInviteRequest(peer=peer,link=p['link'],revoked=True))
        return {'revoked':True,'chat':chat,'link':p['link']}

    async def _importers(self,peer,limit=100):
        f,t,u=_tl()
        found=await self._client(f.messages.GetChatInviteImportersRequest(peer=peer,offset_date=0,offset_user=t.InputUserEmpty(),limit=limit,requested=True))
        users={user.id:user for user in getattr(found,'users',[]) or []}
        return [(item,users.get(item.user_id)) for item in getattr(found,'importers',[]) or []]

    @_operation
    async def list_join_requests(self,chat_id,limit=50):
        entity,chat=await self._resolve(chat_id)
        self._need_admin(entity,'invite_users','join requests')
        rows=[]
        for item,user in await self._importers(await self._client.get_input_entity(entity),_limit(limit,100)):
            date=getattr(item,'date',None)
            rows.append({'user_id':item.user_id,'name':' '.join(filter(None,[getattr(user,'first_name',None),getattr(user,'last_name',None)])) if user else None,
                         'username':getattr(user,'username',None),'requested_at':date.isoformat() if date else None,'about':(getattr(item,'about',None) or '')[:300]})
        return {'chat':chat,'requests':rows,'count':len(rows),'data_trust':UNTRUSTED}

    @_operation
    async def prepare_join_requests_decision(self,chat_id,user_ids,approve):
        ids=_ids(user_ids,50,'user_ids')
        entity,chat=await self._resolve(chat_id)
        self._need_admin(entity,'invite_users','join requests')
        pending={item.user_id:user for item,user in await self._importers(await self._client.get_input_entity(entity))}
        missing=[i for i in ids if i not in pending]
        if missing:
            raise BackendError('no_such_request','No pending request from: '+', '.join(map(str,missing))+'.')
        people=[{'user_id':i,'name':' '.join(filter(None,[getattr(pending[i],'first_name',None),getattr(pending[i],'last_name',None)])) if pending[i] else None,'username':getattr(pending[i],'username',None)} for i in ids]
        preview={'action':'join_requests','chat':chat,'decision':'approve' if approve else 'decline','people':people,'destructive':not approve,'requires_manage_scope':True,'data_trust':UNTRUSTED}
        return self.actions.prepare({'action':'join_requests','chat_id':chat['chat_id'],'user_ids':ids,'approve':bool(approve)},preview)

    async def _run_join_requests(self,p):
        f,t,u=_tl()
        entity,chat=await self._resolve(p['chat_id'])
        peer=await self._client.get_input_entity(entity)
        pending={item.user_id:user for item,user in await self._importers(peer)}

        async def one(item):
            user=pending.get(item['user_id'])
            if user is None:
                return {'status_detail':'no_longer_pending'}
            await self._client(f.messages.HideChatJoinRequestRequest(peer=peer,user_id=u.get_input_user(user),approved=p['approve']))
            return None
        result=await self._each([{'user_id':i,'row':{'user_id':i}} for i in p['user_ids']],one)
        return {**result,'chat':chat,'decision':'approved' if p['approve'] else 'declined'}

    # ---- forum topics, admin log -------------------------------------------------------------------------------

    @_operation
    async def prepare_topic_change(self,chat_id,topic_id,operation,title=None):
        f,t,u=_tl()
        if operation not in TOPIC_OPERATIONS:
            raise BackendError('invalid_input','operation must be one of: '+', '.join(TOPIC_OPERATIONS)+'.')
        if isinstance(topic_id,bool) or not isinstance(topic_id,int) or topic_id<=0:
            raise BackendError('invalid_input','topic_id must be a positive integer (see telegram_get_topics).')
        entity,chat=await self._resolve(chat_id)
        if not getattr(entity,'forum',False):
            raise BackendError('topics_unavailable','The selected chat is not a forum.')
        self._need_admin(entity,'manage_topics','topics')
        name=None
        try:
            found=await self._client(f.messages.GetForumTopicsByIDRequest(peer=await self._client.get_input_entity(entity),topics=[topic_id]))
            name=getattr(next(iter(getattr(found,'topics',[]) or []),None),'title',None)
        except Exception:
            name=None
        if operation=='rename':
            title=_text(title,128,'title').strip()
        preview={'action':'topic_change','operation':operation,'chat':chat,'topic':{'id':topic_id,'title':name},'new_title':title if operation=='rename' else None,
                 'destructive':operation=='delete','effect':{'rename':'The topic is renamed.','close':'Members can no longer write in the topic.','reopen':'The topic is open again.','delete':'The topic and ALL its messages are deleted.'}[operation],'requires_manage_scope':True,'data_trust':UNTRUSTED}
        return self.actions.prepare({'action':'topic_change','chat_id':chat['chat_id'],'topic_id':topic_id,'operation':operation,'title':title},preview)

    async def _run_topic_change(self,p):
        f,t,u=_tl()
        entity,chat=await self._resolve(p['chat_id'])
        peer=await self._client.get_input_entity(entity)
        if p['operation']=='delete':
            await self._client(f.messages.DeleteTopicHistoryRequest(peer=peer,top_msg_id=p['topic_id']))
        else:
            await self._client(f.messages.EditForumTopicRequest(peer=peer,topic_id=p['topic_id'],title=p['title'] if p['operation']=='rename' else None,
                                                                 closed={'close':True,'reopen':False}.get(p['operation'])))
        return {'changed':True,'operation':p['operation'],'chat':chat,'topic_id':p['topic_id']}

    @_operation
    async def get_admin_log(self,chat_id,limit=30):
        f,t,u=_tl()
        entity,chat=await self._resolve(chat_id)
        if type(entity).__name__!='Channel':
            raise BackendError('unsupported_operation','The admin log exists for channels and supergroups.')
        self._need_admin(entity,None,'the admin log')
        found=await self._client(f.channels.GetAdminLogRequest(channel=u.get_input_channel(await self._client.get_input_entity(entity)),q='',max_id=0,min_id=0,limit=_limit(limit,100)))
        users={user.id:' '.join(filter(None,[getattr(user,'first_name',None),getattr(user,'last_name',None)])) or getattr(user,'username',None) for user in getattr(found,'users',[]) or []}
        rows=[{'event_id':event.id,'date':event.date.isoformat() if getattr(event,'date',None) else None,'by':users.get(event.user_id),'user_id':event.user_id,
               'action':type(event.action).__name__.replace('ChannelAdminLogEventAction','')} for event in getattr(found,'events',[]) or []]
        return {'chat':chat,'events':rows,'count':len(rows),'data_trust':UNTRUSTED}

    # ---- deleting an owned chat, blocking ---------------------------------------------------------------------

    @_operation
    async def prepare_delete_chat_for_all(self,chat_id,confirm_title):
        entity,chat=await self._resolve(chat_id)
        if chat_type(entity) not in ('group','supergroup','channel'):
            raise BackendError('invalid_input','Select a group or channel.')
        if not getattr(entity,'creator',False):
            raise BackendError('permission_denied','Only the owner of a group or channel can delete it for everyone. Use telegram_prepare_leave_chats to leave.')
        title=getattr(entity,'title','')
        if confirm_title!=title:
            raise BackendError('confirm_title_mismatch','To delete this chat pass its exact title as confirm_title.',diagnostics={'title':title})
        preview={'action':'delete_chat_for_all','chat':chat,'members':getattr(entity,'participants_count',None),'destructive':True,'irreversible':True,
                 'effect':'The chat, its members and ALL its messages are deleted for everyone. This cannot be undone.','requires_manage_scope':True}
        return self.actions.prepare({'action':'delete_chat_for_all','chat_id':chat['chat_id'],'title':title},preview)

    async def _run_delete_for_all(self,p):
        f,t,u=_tl()
        entity,chat=await self._resolve(p['chat_id'])
        if not getattr(entity,'creator',False) or getattr(entity,'title','')!=p['title']:
            raise BackendError('confirm_title_mismatch','The chat changed since the preview; nothing was deleted.')
        peer=await self._client.get_input_entity(entity)
        if type(entity).__name__=='Channel':
            await self._client(f.channels.DeleteChannelRequest(channel=u.get_input_channel(peer)))
        else:
            await self._client(f.messages.DeleteChatRequest(chat_id=entity.id))
        cleanup=await self._folders_drop_peers({chat['chat_id']})
        return {'deleted':True,'chat':chat,**cleanup}

    @_operation
    async def prepare_block_user(self,user,block=True):
        client=await self._ready()
        if isinstance(user,bool):
            raise BackendError('invalid_input','Pass a user ID from your chats or a @username.')
        if isinstance(user,int):
            dialogs=await self._dialog_map()
            if user not in dialogs:
                raise BackendError('chat_not_found','This user is not in your chat list; pass their @username.')
            entity=dialogs[user].entity
        elif isinstance(user,str) and re.fullmatch(r'@?[A-Za-z][A-Za-z0-9_]{3,31}',user.strip()):
            try:
                entity=await client.get_entity(user.strip())
            except Exception as error:
                if type(error).__name__ in ('UsernameNotOccupiedError','UsernameInvalidError') or isinstance(error,ValueError):
                    raise BackendError('user_not_accessible','No such user is visible to this account.') from None
                raise
        else:
            raise BackendError('invalid_input','Pass a user ID from your chats or a @username.')
        if type(entity).__name__!='User':
            raise BackendError('invalid_input','Only people and bots can be blocked; leave groups with telegram_prepare_leave_chats.')
        self._known_users[entity.id]=entity
        preview={'action':'block_user','operation':'block' if block else 'unblock','user':{'user_id':entity.id,'name':' '.join(filter(None,[getattr(entity,'first_name',None),getattr(entity,'last_name',None)])),
                 'username':getattr(entity,'username',None),'bot':bool(getattr(entity,'bot',False))},'effect':'They can no longer write to you or see your status.' if block else 'They can write to you again.',
                 'reversible':True,'requires_manage_scope':True,'data_trust':UNTRUSTED}
        return self.actions.prepare({'action':'block_user','user_id':entity.id,'username':getattr(entity,'username',None),'block':bool(block)},preview)

    async def _run_block_user(self,p):
        f,t,u=_tl()
        client=self._client
        if p['user_id'] in self._known_users:
            peer=await client.get_input_entity(self._known_users[p['user_id']])
        else:
            peer=await client.get_input_entity('@'+p['username']) if p.get('username') else await client.get_input_entity(p['user_id'])
        await client(f.contacts.BlockRequest(id=peer) if p['block'] else f.contacts.UnblockRequest(id=peer))
        return {'changed':True,'blocked':p['block'],'user_id':p['user_id']}
