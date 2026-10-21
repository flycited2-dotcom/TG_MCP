"""A deliberately slow, capped message to a reviewed list of groups (for example the chats of one folder).

One approval covers the whole list and the exact text. Posting runs in the background with random pauses, daily
limits set by the server owner, a refusal to repeat the same text in the same chat soon, and a full stop when
Telegram signals flooding or a send outcome is unknown.
"""
from __future__ import annotations
import hashlib
import secrets

from telegram_backend import BackendError,_operation,_text
from bot_interaction import UNTRUSTED
from chat_discovery import chat_type

DEFAULT_PAUSE=(60,180)


def _tl():
    from telethon.tl import functions,types
    from telethon import utils
    return functions,types,utils


def can_post(entity,media=False):
    """(ok, reason) judged from what the chat list already tells us about our rights there."""
    name=type(entity).__name__
    if name=='User':
        return False,'not_a_group'
    if getattr(entity,'left',False) or getattr(entity,'deactivated',False):
        return False,'not_a_member'
    admin=getattr(entity,'admin_rights',None)
    creator=bool(getattr(entity,'creator',False))
    if getattr(entity,'broadcast',False):
        return (True,None) if creator or (admin is not None and getattr(admin,'post_messages',False)) else (False,'channel_readonly')
    if creator or admin is not None:
        return True,None
    personal=getattr(entity,'banned_rights',None)
    default=getattr(entity,'default_banned_rights',None)
    if personal is not None and getattr(personal,'send_messages',False):
        return False,'restricted_for_you'
    if default is not None and getattr(default,'send_messages',False):
        return False,'members_cannot_post'
    if media and default is not None and (getattr(default,'send_media',False) or getattr(default,'send_photos',False)):
        return False,'media_not_allowed'
    return True,None


class Campaigns:
    """Mixed into ExtendedBackend (needs Jobs, Folders and ChatDiscovery)."""

    @_operation
    async def prepare_campaign(self,text='',chat_ids=None,folder=None,file_id=None,parse_mode='plain',pause_min_s=None,pause_max_s=None):
        if (chat_ids is None)==(folder is None):
            raise BackendError('invalid_input','Choose the chats with exactly one of chat_ids or folder.')
        info=self.files.metadata(file_id) if file_id else None
        if info is not None and not info['mime_type'].startswith(('image/','video/')):
            raise BackendError('unsupported_media','A campaign message carries text, a photo or a video.')
        text=_text(text,1024 if info else 4096,'text',allow_empty=bool(info))
        low=max(self.limits.min_delay,DEFAULT_PAUSE[0]) if pause_min_s is None else int(pause_min_s)
        high=low+(DEFAULT_PAUSE[1]-DEFAULT_PAUSE[0]) if pause_max_s is None else int(pause_max_s)
        if low<self.limits.min_delay or high<low or high>3600:
            raise BackendError('invalid_input',f'The pause between messages must be at least {self.limits.min_delay} s (set by the server owner), the maximum not below the minimum and at most 3600 s.')
        ids=await self._ordered_folder_ids(folder) if folder is not None else list(dict.fromkeys(chat_ids))
        if not ids:
            raise BackendError('invalid_input','No chats selected.')
        if len(ids)>self.limits.campaign_max_chats:
            raise BackendError('too_many_chats_selected',f'At most {self.limits.campaign_max_chats} chats per campaign (set by the server owner); split the list.')
        dialogs=await self._dialog_map()
        digest=hashlib.sha256((text+'\n'+(info['sha256'] if info else '')).encode()).hexdigest()
        rows,steps=[],[]
        for chat_id in ids:
            if isinstance(chat_id,bool) or not isinstance(chat_id,int):
                raise BackendError('invalid_input','chat_ids must be numeric chat IDs.')
            dialog=dialogs.get(chat_id)
            if dialog is None:
                rows.append({'chat_id':chat_id,'status':'skip','reason':'not_in_your_chat_list'})
                continue
            row={**self._row(dialog.entity,dialog),'chat_id':chat_id}
            ok,reason=can_post(dialog.entity,media=bool(info))
            if ok and self._ledger().sent_recently(chat_id,digest,self.limits.dedupe_days):
                ok,reason=False,'same_text_sent_recently'
            if ok:
                row['status']='will_send'
                steps.append({'chat_id':chat_id,'title':row['title'],'random_id':secrets.randbits(62)+1})
            else:
                row.update({'status':'skip','reason':reason})
            rows.append(row)
        if not steps:
            raise BackendError('nothing_to_send','No chat in this list can receive the message.',diagnostics={'chats':rows})
        self.check_quota('message',len(steps),self.limits.message_daily)
        media=[{'file_id':file_id,'kind':'photo' if info['mime_type'].startswith('image/') else 'video','caption':'','original':False}] if info else []
        preview={'action':'campaign','text':text,'media':self._media_preview(media),'chats':rows,'will_send':len(steps),'skipped':len(rows)-len(steps),
                 'pause_between_seconds':[low,high],'estimated_minutes':round(len(steps)*(low+high)/2/60),
                 'limits':{**self.limits.as_dict(),'messages_left_today_after':self.limits.message_daily-self._ledger().used('message')-len(steps)},
                 'safeguards':['random pauses between chats','stops completely on a Telegram flood or spam warning','never repeats a send whose result is unknown','refuses the same text in the same chat again within '+str(self.limits.dedupe_days)+' days','can be cancelled with telegram_job_cancel'],
                 'warning':'Posting the same advertisement to many groups can get an account restricted. Post only where the group rules allow it (see telegram_inspect_chat) and keep lists short.',
                 'requires_explicit_confirmation':True,'data_trust':UNTRUSTED}
        payload={'action':'campaign','steps':steps,'text':text,'media':media,'digest':digest,'options':{'parse_mode':parse_mode,'link_preview':False,'silent':False},'pause':[low,high]}
        return self.actions.prepare(payload,preview)

    async def _ordered_folder_ids(self,folder):
        return self._peer_ids(self._find_folder(await self._filters(),folder))

    async def _start_campaign(self,payload):
        async def runner(step):
            return await self._campaign_step(step,payload)
        job=await self.start_job('campaign',payload['steps'],runner,tuple(payload['pause']))
        return {'started':True,'job_id':job,'total':len(payload['steps']),'pause_between_seconds':payload['pause'],
                'note':'Posting runs in the background with pauses. Check telegram_job_status; cancel with telegram_job_cancel.'}

    async def _campaign_step(self,step,payload):
        f,t,u=_tl()
        from extended_backend import returned_ids
        self.check_quota('message',1,self.limits.message_daily)

        async def operation():
            client=await self._ready()
            entity,chat=await self._resolve(step['chat_id'])
            ok,reason=can_post(entity,media=bool(payload['media']))
            if not ok:
                return {'status':'skipped','reason':reason}
            peer=await client.get_input_entity(entity)
            text,entities=self._parsed(payload['text'],payload['options']['parse_mode'])
            try:
                if payload['media']:
                    media=await self._media(payload['media'][0],peer)
                    result=await client(f.messages.SendMediaRequest(peer=peer,media=media,message=text,entities=entities,random_id=step['random_id']))
                else:
                    result=await client(f.messages.SendMessageRequest(peer=peer,message=text,entities=entities,random_id=step['random_id'],no_webpage=True))
            except Exception as error:
                name=type(error).__name__
                if name in ('ChatWriteForbiddenError','UserBannedInChannelError','ChannelPrivateError','ChatRestrictedError','ChatAdminRequiredError'):
                    return {'status':'skipped','reason':'no_permission'}
                if name=='SlowModeWaitError':
                    return {'status':'skipped','reason':'slow_mode','retry_after_seconds':int(getattr(error,'seconds',0) or 0)}
                raise
            ids=returned_ids(result)
            if not ids:
                raise BackendError('result_unconfirmed','Telegram accepted the send but returned no message ID; the job stops. Check the chat before doing anything else.')
            return {'status':'sent','message_id':ids[0]}

        outcome=await self._step_retry(lambda:self._locked_step(operation))
        if outcome['status']=='sent':
            self._ledger().add('message',step['chat_id'],payload['digest'])
        self.audit.record('campaign_message',step['chat_id'],outcome.get('message_id'),outcome['status'])
        return outcome
