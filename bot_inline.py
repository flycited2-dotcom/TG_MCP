"""Inline mode of bots (TZ-bot-interaction 4.4): query `@bot text` in a chat, then send one chosen result."""
from __future__ import annotations
import hashlib
import secrets

from telegram_backend import BackendError,_operation,_text,_user
from bot_interaction import UNTRUSTED

REF_TTL=600


def _tl():
    from telethon.tl import functions,types
    from telethon import utils
    return functions,types,utils


class InlineBots:
    """Mixed into ExtendedBackend. Result handles live in memory: a restart simply requires a new query."""

    def _inline_cache(self):
        if not hasattr(self,'_inline_results'):
            self._inline_results={}
        now=self._clock()
        for key in [key for key,value in self._inline_results.items() if value['expires']<=now]:
            del self._inline_results[key]
        return self._inline_results

    @_operation
    async def inline_query(self,chat_id,bot,query,offset=''):
        f,t,u=_tl()
        client=await self._ready()
        entity,chat=await self._resolve(chat_id)
        user=await self._bot_entity(bot)
        if not getattr(user,'bot',False):
            raise BackendError('not_a_bot','The selected account is not a bot.')
        if getattr(user,'bot_inline_placeholder',None) is None:
            raise BackendError('inline_not_supported','This bot does not support inline mode.')
        query=_text(query,512,'query',True)
        found=await client(f.messages.GetInlineBotResultsRequest(bot=u.get_input_user(await client.get_input_entity(user)),peer=await client.get_input_entity(entity),query=query,offset=str(offset or '')))
        cache=self._inline_cache()
        expires=self._clock()+min(REF_TTL,max(60,int(getattr(found,'cache_time',0) or 300)))
        items=[]
        for result in list(getattr(found,'results',None) or [])[:50]:
            ref='r_'+hashlib.sha256(f"{chat['chat_id']}:{user.id}:{found.query_id}:{result.id}".encode()).hexdigest()[:20]
            message=getattr(result,'send_message',None)
            shown={'result_ref':ref,'type':getattr(result,'type',None),'title':getattr(result,'title',None),'description':getattr(result,'description',None)}
            text=getattr(message,'message',None)
            if text:
                shown['text_preview']=text[:300]
            cache[ref]={'chat_id':chat['chat_id'],'bot_id':user.id,'bot_username':getattr(user,'username',None),'query_id':found.query_id,'result_id':result.id,
                        'expires':expires,'title':shown['title'],'type':shown['type'],'text':text}
            items.append(shown)
        self._known_users[user.id]=user
        return {'chat':chat,'bot':_user(user),'query':query,'results':items,'next_offset':getattr(found,'next_offset',None) or None,
                'valid_for_seconds':int(expires-self._clock()),'data_trust':UNTRUSTED}

    @_operation
    async def prepare_send_inline_result(self,chat_id,result_ref,options=None):
        entity,chat=await self._resolve(chat_id)
        stored=self._inline_cache().get(result_ref)
        if stored is None:
            raise BackendError('inline_result_unknown','This result is unknown or expired. Run telegram_inline_query again.')
        if stored['chat_id']!=chat['chat_id']:
            raise BackendError('invalid_input','This result was fetched for a different chat; query again in the target chat.')
        options=self._options(options)
        if options['schedule_at']:
            raise BackendError('invalid_input','Inline results cannot be scheduled here.')
        if options['reply_to']:
            await self._check_messages(entity,chat,[options['reply_to']])
        preview={'action':'send_inline_result','recipient':chat,'bot':{'id':stored['bot_id'],'username':stored['bot_username']},
                 'result':{'type':stored['type'],'title':stored['title'],'text':stored['text']},'options':options,
                 'note':'The message is sent through this bot (shown as "via @bot").','data_trust':UNTRUSTED}
        payload={'action':'send_inline_result','chat_id':chat['chat_id'],'query_id':stored['query_id'],'result_id':stored['result_id'],
                 'options':options,'random_id':secrets.randbits(62)+1}
        return self.actions.prepare(payload,preview)

    async def _send_inline(self,p,peer):
        f,t,u=_tl()
        options=p['options']
        try:
            return await self._client(f.messages.SendInlineBotResultRequest(peer=peer,random_id=p['random_id'],query_id=p['query_id'],id=p['result_id'],
                                      silent=options['silent'],reply_to=self._reply(options)))
        except Exception as error:
            if type(error).__name__ in ('QueryIdInvalidError','ResultIdInvalidError'):
                failure=BackendError('inline_result_expired','Telegram no longer accepts this inline result. Nothing was sent; run telegram_inline_query again.')
                failure.retry_safe=True
                raise failure from None
            raise
