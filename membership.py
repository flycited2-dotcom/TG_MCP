"""Joining and leaving chats on the owner's behalf: one approval for a short list, executed as a paced background job."""
from __future__ import annotations

from telegram_backend import BackendError,_operation,_safe_error
from bot_interaction import UNTRUSTED
from chat_discovery import parse_target,chat_type

JOIN_PAUSE=(20,60)
LEAVE_PAUSE=(5,15)
MAX_JOIN=50
MAX_LEAVE=30


def _tl():
    from telethon.tl import functions,types
    from telethon import utils
    return functions,types,utils


class Membership:
    """Mixed into ExtendedBackend (needs Jobs, ChatDiscovery and Folders)."""

    async def _join_row(self,target):
        """What joining would do, without joining. Errors become an 'invalid' row instead of failing the whole list."""
        try:
            parsed=parse_target(target)
            info=await self.inspect_chat_unlocked(target)
        except Exception as error:
            failure=error if isinstance(error,BackendError) else _safe_error(error)
            return {'target':target,'status':'invalid','reason':failure.code,'message':str(failure)}
        row={'target':target,'title':info.get('title'),'type':info.get('type'),'members':info.get('members'),'requires_approval':bool(info.get('requires_approval')),
             'warnings':info.get('warnings',[]),'status':'already_member' if info.get('member') else 'will_join'}
        row.update({'kind':parsed['kind'],**({'name':parsed['name']} if parsed['kind']=='username' else {'hash':parsed['hash']})})
        return row

    @_operation
    async def prepare_join_chats(self,targets):
        if not isinstance(targets,list) or not 1<=len(targets)<=MAX_JOIN:
            raise BackendError('invalid_input',f'Pass 1..{MAX_JOIN} chats per approval.')
        rows=[await self._join_row(target) for target in dict.fromkeys(targets)]
        joining=[row for row in rows if row['status']=='will_join']
        if not joining:
            raise BackendError('nothing_to_join','No chat in this list can be joined.',diagnostics={'chats':rows})
        self.check_quota('join',len(joining),self.limits.join_daily)
        steps=[{'kind':row['kind'],'target':row['target'],'title':row['title'],**({'name':row['name']} if row['kind']=='username' else {'hash':row['hash']})} for row in joining]
        shown=[{key:value for key,value in row.items() if key not in ('hash',)} for row in rows]
        preview={'action':'join_chats','chats':shown,'will_join':len(joining),'requires_approval_count':sum(1 for row in joining if row['requires_approval']),
                 'pause_between_seconds':list(JOIN_PAUSE),'limits':{'joins_left_today_after':self.limits.join_daily-self._ledger().used('join')-len(joining) if self.limits.join_daily else 'unlimited'},
                 'effect':'Joins as the account owner; some groups only receive a join request. Joins run one by one in the background.',
                 'requires_manage_scope':True,'data_trust':UNTRUSTED}
        payload={'action':'join_chats','steps':steps}
        return self.actions.prepare(payload,preview)

    async def inspect_chat_unlocked(self,target):
        """inspect_chat without taking the account lock again (the caller already holds it)."""
        return await self.inspect_chat.__wrapped__(self,target)

    async def _start_join(self,payload):
        async def runner(step):
            return await self._join_step(step)
        job=await self.start_job('join_chats',payload['steps'],runner,JOIN_PAUSE)
        return {'started':True,'job_id':job,'total':len(payload['steps']),'note':'Running in the background. Check telegram_job_status.'}

    async def _join_step(self,step):
        f,t,u=_tl()
        self.check_quota('join',1,self.limits.join_daily)

        async def operation():
            client=await self._ready()
            try:
                if step['kind']=='invite':
                    result=await client(f.messages.ImportChatInviteRequest(step['hash']))
                else:
                    entity=await client.get_entity('@'+step['name'])
                    result=await client(f.channels.JoinChannelRequest(u.get_input_channel(await client.get_input_entity(entity))))
            except Exception as error:
                name=type(error).__name__
                if name=='InviteRequestSentError':
                    return {'status':'request_sent'}
                if name=='UserAlreadyParticipantError':
                    return {'status':'already_member'}
                if name in ('InviteHashExpiredError','InviteHashInvalidError','InviteHashEmptyError'):
                    return {'status':'failed','error':'invite_invalid'}
                if name in ('UsernameNotOccupiedError','UsernameInvalidError','ChannelPrivateError','ChannelInvalidError'):
                    return {'status':'failed','error':'not_found_or_private'}
                if name in ('ChannelsTooMuchError','UserChannelsTooMuchError'):
                    raise BackendError('too_many_chats','You are a member of the maximum number of chats; leave some first.') from None
                raise
            chats=getattr(result,'chats',None) or []
            joined={'status':'joined'}
            if chats:
                joined.update({'chat_id':u.get_peer_id(chats[0]),'title':getattr(chats[0],'title',None)})
            return joined

        outcome=await self._step_retry(lambda:self._locked_step(operation))
        if outcome['status'] in ('joined','request_sent'):
            self._ledger().add('join',outcome.get('chat_id'))
        self.audit.record('join_chat',outcome.get('chat_id'),None,outcome['status'])
        return outcome

    @_operation
    async def prepare_leave_chats(self,chat_ids,clear_history=True):
        if not isinstance(chat_ids,list) or not 1<=len(chat_ids)<=MAX_LEAVE:
            raise BackendError('invalid_input',f'Pass 1..{MAX_LEAVE} chats per approval.')
        dialogs=await self._dialog_map()
        folders=await self._filters()
        rows,steps=[],[]
        for chat_id in dict.fromkeys(chat_ids):
            if isinstance(chat_id,bool) or not isinstance(chat_id,int):
                raise BackendError('invalid_input','chat_ids must be numeric chat IDs.')
            dialog=dialogs.get(chat_id)
            if dialog is None:
                rows.append({'chat_id':chat_id,'status':'skip','reason':'not_in_your_chat_list'})
                continue
            entity=dialog.entity
            kind=chat_type(entity)
            row={**self._row(entity,dialog),'chat_id':chat_id,'in_folders':[self._folder_name(item) for item in folders if chat_id in self._peer_ids(item)]}
            if kind in ('private','bot'):
                row.update({'status':'skip','reason':'only_groups_and_channels'})
            elif getattr(entity,'creator',False):
                row.update({'status':'skip','reason':'you_are_the_owner'})
            else:
                row['status']='will_leave'
                steps.append({'chat_id':chat_id,'title':row['title'],'clear_history':bool(clear_history)})
            rows.append(row)
        if not steps:
            raise BackendError('nothing_to_leave','None of these chats can be left.',diagnostics={'chats':rows})
        self.check_quota('leave',len(steps),self.limits.leave_daily)
        preview={'action':'leave_chats','chats':rows,'will_leave':len(steps),'clear_history':bool(clear_history),'destructive':True,'pause_between_seconds':list(LEAVE_PAUSE),
                 'effect':'You leave these chats'+(' and their history is deleted from your account' if clear_history else '')+'; they are removed from your folders. Rejoining may need an invite or approval.',
                 'cache_note':'Telegram keeps no per-chat cache on the server. Leaving removes the chat from your list, and the history deletion removes your copy; the Telegram app clears its own local cache by itself.',
                 'requires_manage_scope':True,'data_trust':UNTRUSTED}
        payload={'action':'leave_chats','steps':steps}
        return self.actions.prepare(payload,preview)

    @staticmethod
    def _folder_name(folder):
        from folders import _folder_title
        return _folder_title(folder)

    async def _start_leave(self,payload):
        async def runner(step):
            return await self._leave_step(step)
        job=await self.start_job('leave_chats',payload['steps'],runner,LEAVE_PAUSE)
        return {'started':True,'job_id':job,'total':len(payload['steps']),'note':'Running in the background. Check telegram_job_status.'}

    async def _leave_step(self,step):
        f,t,u=_tl()
        self.check_quota('leave',1,self.limits.leave_daily)

        async def operation():
            client=await self._ready()
            entity,chat=await self._resolve(step['chat_id'])
            peer=await client.get_input_entity(entity)
            try:
                if type(entity).__name__=='Channel':
                    await client(f.channels.LeaveChannelRequest(u.get_input_channel(peer)))
                else:
                    await client(f.messages.DeleteChatUserRequest(chat_id=entity.id,user_id=t.InputUserSelf(),revoke_history=False))
                    if step['clear_history']:
                        await client(f.messages.DeleteHistoryRequest(peer=peer,max_id=0,just_clear=False,revoke=False))
            except Exception as error:
                name=type(error).__name__
                if name in ('UserNotParticipantError','ChannelPrivateError'):
                    return {'status':'already_left'}
                if name=='UserCreatorError':
                    return {'status':'failed','error':'you_are_the_owner'}
                raise
            return {'status':'left','history_deleted':bool(step['clear_history'])}

        outcome=await self._step_retry(lambda:self._locked_step(operation))
        if outcome['status'] in ('left','already_left'):
            cleanup=await self._locked_step(lambda:self._folders_drop_peers({step['chat_id']}))
            outcome.update(cleanup)
            if outcome['status']=='left':
                self._ledger().add('leave',step['chat_id'])
        self.audit.record('leave_chat',step['chat_id'],None,outcome['status'])
        return outcome
