"""@BotFather management for the owner's own bots (TZ-bot-interaction 4.8), off unless TG_ALLOW_BOTFATHER=true.

Never possible: /token, /revoke, /deletebot, transferring or creating bots. Tokens in any BotFather
message are replaced by [token redacted] before an agent can see them.
"""
from __future__ import annotations
import asyncio
import re
import secrets
import time

from telegram_backend import BackendError,_operation,_text
from bot_interaction import UNTRUSTED

BOTFATHER_ID=93372553
TOKEN=re.compile(r'\b\d{6,12}:[A-Za-z0-9_-]{30,}\b')
SCENARIOS={'set_description':('/setdescription',512),'set_about':('/setabouttext',120),'set_name':('/setname',64)}
REPLY_WAIT=15
SETTLE_SECONDS=0.8
POLL_SECONDS=0.5
ERRORS=('invalid bot selected','sorry','invalid','too long','can\'t','cannot','unrecognized')


def redact(text):
    return TOKEN.sub('[token redacted]',text) if isinstance(text,str) else text


def is_botfather(chat):
    return bool(chat) and (chat.get('chat_id')==BOTFATHER_ID or (chat.get('username') or '').casefold()=='botfather')


def guard_recipient(chat):
    """General message tools may never talk to BotFather: only the reviewed scenario tool may."""
    if is_botfather(chat):
        raise BackendError('botfather_scenario_required','Messages to @BotFather are sent only through telegram_prepare_botfather_change, and only when the server owner enabled TG_ALLOW_BOTFATHER. /token, /revoke and /deletebot are never possible.')


class BotFatherOps:
    """Mixed into ExtendedBackend."""

    @_operation
    async def prepare_botfather_change(self,bot,operation,text=None,commands=None,file_id=None):
        if not self.allow_botfather:
            raise BackendError('botfather_disabled','Managing bots through @BotFather is disabled on this server (TG_ALLOW_BOTFATHER=false). Only the server owner can enable it.')
        if not isinstance(bot,str) or not re.fullmatch(r'@?[A-Za-z][A-Za-z0-9_]{2,30}[Bb]ot',bot.strip()):
            raise BackendError('invalid_input','Pass the bot @username (it ends with "bot").')
        username='@'+bot.strip().lstrip('@')
        if operation in SCENARIOS:
            command,limit=SCENARIOS[operation]
            value=_text(text,limit,'text')
            if value.lstrip().startswith('/'):
                raise BackendError('forbidden_botfather_command','A value starting with "/" would be read by @BotFather as a command (such as /token or /deletebot) and is refused.')
            steps=[{'send':command},{'send':username},{'send':value}]
        elif operation=='set_commands':
            entries=[]
            for item in commands or []:
                name=item.get('command','').lstrip('/')
                if not re.fullmatch(r'[a-z0-9_]{1,32}',name) or not 3<=len(item.get('description','').strip())<=256:
                    raise BackendError('invalid_input','Each command needs a lowercase name (a-z, 0-9, _) and a description of 3..256 characters.')
                entries.append(f"{name} - {item['description'].strip()}")
            if not 1<=len(entries)<=100:
                raise BackendError('invalid_input','Provide 1..100 commands.')
            steps=[{'send':'/setcommands'},{'send':username},{'send':'\n'.join(entries)}]
        elif operation=='set_userpic':
            info=self.files.metadata(file_id or '')
            if info['mime_type'] not in ('image/jpeg','image/png'):
                raise BackendError('unsupported_media','A bot picture must be a JPEG or PNG image.')
            steps=[{'send':'/setuserpic'},{'send':username},{'file_id':file_id,'filename':info['filename'],'sha256':info['sha256']}]
        else:
            raise BackendError('invalid_input','Operation must be one of: set_description, set_about, set_name, set_commands, set_userpic.')
        _,chat=await self._botfather_chat()
        preview={'action':'botfather','operation':operation,'recipient':chat,'bot':username,'messages_in_order':steps,
                 'effect':'These messages are sent to @BotFather one by one; each reply is awaited before the next. Tokens in replies are never shown.',
                 'requires_manage_scope':True,'data_trust':UNTRUSTED}
        payload={'action':'botfather','operation':operation,'chat_id':chat['chat_id'],'steps':steps,'random_id':secrets.randbits(31)}
        return self.actions.prepare(payload,preview)

    async def _botfather_chat(self):
        client=await self._ready()
        entity=await client.get_entity('BotFather')
        return entity,{'chat_id':BOTFATHER_ID,'title':getattr(entity,'first_name',None) or 'BotFather','username':'BotFather','type':'private'}

    async def _botfather_reply(self,entity,after_id):
        deadline=time.monotonic()+REPLY_WAIT
        while True:
            fresh=[m async for m in self._client.iter_messages(entity,limit=10) if m.id>after_id and not getattr(m,'out',False)]
            if fresh:
                await asyncio.sleep(SETTLE_SECONDS)
                fresh=[m async for m in self._client.iter_messages(entity,limit=10) if m.id>after_id and not getattr(m,'out',False)] or fresh
                return redact('\n'.join((getattr(m,'message','') or '') for m in sorted(fresh,key=lambda m:m.id)))
            if time.monotonic()>=deadline:
                return None
            await asyncio.sleep(POLL_SECONDS)

    async def _botfather(self,p):
        from telethon.tl import functions as f
        entity,chat=await self._botfather_chat()
        peer=await self._client.get_input_entity(entity)
        steps,transcript=p['steps'],[]
        for index,step in enumerate(steps):
            latest=[m.id async for m in self._client.iter_messages(entity,limit=1)]
            after=latest[0] if latest else 0
            if 'send' in step:
                await self._client(f.messages.SendMessageRequest(peer=peer,message=step['send'],random_id=secrets.randbits(62)+1))
            else:
                media=await self._media({'file_id':step['file_id'],'kind':'photo','original':False},peer)
                await self._client(f.messages.SendMediaRequest(peer=peer,media=media,message='',random_id=secrets.randbits(62)+1))
            reply=await self._botfather_reply(entity,after)
            transcript.append({'step':index+1,'sent':step.get('send') or '[image]','reply':(reply or '')[:300] if reply is not None else None})
            if reply is None or any(marker in reply.lower() for marker in ERRORS) and 'success' not in reply.lower():
                await self._client(f.messages.SendMessageRequest(peer=peer,message='/cancel',random_id=secrets.randbits(62)+1))
                raise BackendError('botfather_error','@BotFather did not accept step '+str(index+1)+'; the dialog was cancelled with /cancel. Check the bot name and ownership. Do not retry blindly.',
                                   diagnostics={'steps':transcript})
        last=(transcript[-1]['reply'] or '').lower()
        return {'changed':'success' in last,'confirmed_by_botfather':'success' in last,'operation':p['operation'],'chat':chat,'steps':transcript}
