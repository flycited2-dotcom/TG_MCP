"""Channel publications (TZ-bot-interaction 4.6): publish, edit text/media, delete, pin, stats.

Telegram rule handled honestly: a message a bot sent to a PRIVATE chat can be edited only by that bot,
while an administrator with the right may edit posts of a channel.
"""
from __future__ import annotations
import difflib
import re
from urllib.parse import urlsplit

from telegram_backend import BackendError,_operation,_text
from bot_interaction import UNTRUSTED


def word_diff(old,new):
    """Segments marking what disappeared (delete) and what appeared (insert)."""
    a=re.findall(r'\s+|\S+',old or '')
    b=re.findall(r'\s+|\S+',new or '')
    segments=[]
    for tag,i1,i2,j1,j2 in difflib.SequenceMatcher(None,a,b,autojunk=False).get_opcodes():
        if tag=='equal':
            segments.append({'op':'equal','text':''.join(a[i1:i2])})
        else:
            if i2>i1:
                segments.append({'op':'delete','text':''.join(a[i1:i2])})
            if j2>j1:
                segments.append({'op':'insert','text':''.join(b[j1:j2])})
    return segments


def media_kind(message):
    if getattr(message,'photo',None):
        return 'photo'
    if getattr(message,'video',None) or getattr(message,'gif',None):
        return 'video'
    return 'document' if getattr(message,'media',None) else None


class ChannelPosts:
    """Mixed into ExtendedBackend."""

    @staticmethod
    def _markup(buttons):
        if not buttons:
            return None
        from telethon.tl import types as t
        return t.ReplyInlineMarkup(rows=[t.KeyboardInlineButtonRow(buttons=[t.KeyboardInlineButton(text=item['text'],type=t.InlineButtonTypeUrl(url=item['url']))]) for item in buttons])

    @staticmethod
    def _url_buttons(items):
        buttons=[]
        for item in items or []:
            text=_text(item.get('text'),64,'button text')
            url=item.get('url')
            parsed=urlsplit(url) if isinstance(url,str) else None
            if not parsed or parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username or any(c.isspace() for c in url) or len(url)>2048:
                raise BackendError('invalid_input','Button URLs must be plain http(s) links.')
            buttons.append({'text':text,'url':url})
        if len(buttons)>8:
            raise BackendError('invalid_input','At most 8 link buttons.')
        return buttons

    @staticmethod
    def _editable(chat,message):
        if chat['type'] in ('private','group') and not getattr(message,'out',False):
            raise BackendError('not_editable_by_user','A message sent by someone else (such as a bot in a private chat) can be edited only by its author. Use the bot\'s buttons or commands instead.')

    async def _channel(self,chat_id):
        entity,chat=await self._resolve(chat_id)
        if chat['type']!='channel':
            raise BackendError('invalid_input','Select a channel. For ordinary chats and groups use the general message tools.')
        return entity,chat

    @_operation
    async def prepare_publish_post(self,chat_id,text='',file_id=None,url_buttons=None,schedule_at=None,silent=False,parse_mode='plain'):
        entity,chat=await self._channel(chat_id)
        buttons=self._url_buttons(url_buttons)
        media=[]
        if file_id:
            info=self.files.metadata(file_id)
            if not info['mime_type'].startswith(('image/','video/')):
                raise BackendError('unsupported_media','A channel post carries a photo or video; send other files with telegram_send_file.')
            media=[{'file_id':file_id,'kind':'photo' if info['mime_type'].startswith('image/') else 'video','caption':'','original':False}]
        options={'reply_to':None,'topic_id':None,'parse_mode':parse_mode,'link_preview':False,'silent':bool(silent),'schedule_at':schedule_at}
        preview,payload=await self._build_send(chat_id,text,media,options)
        preview.update({'publication_type':'channel_post','posts_as':'the channel','buttons':buttons})
        payload['buttons']=buttons
        payload['publication_type']='channel_post'
        if buttons:
            preview['buttons_note']='Telegram may refuse link buttons from a user account; then nothing is published and you get buttons_not_supported.'
        return self.actions.prepare(payload,preview)

    @_operation
    async def prepare_edit_post_text(self,chat_id,message_id,text,parse_mode='plain'):
        entity,chat=await self._resolve(chat_id)
        message=await self._fetch_message(entity,chat,message_id)
        self._editable(chat,message)
        has_media=bool(getattr(message,'media',None))
        text=_text(text,1024 if has_media else 4096,'text')
        old=getattr(message,'message','') or ''
        if text==old:
            raise BackendError('no_change','The post already has exactly this text.')
        preview={'action':'edit_post','chat':chat,'message_id':message.id,'was':old,'now':text,'diff':word_diff(old,text),'has_media':has_media,
                 'requires_manage_scope':True,'formatting_note':'Bold/links of the old text are replaced; use parse_mode html or markdown to format the new text.',
                 'buttons_note':'Telegram may drop inline buttons from a post edited by a user account.','data_trust':UNTRUSTED}
        payload={'action':'edit_post','chat_id':chat['chat_id'],'message_ids':[message.id],'text':text,'options':{'parse_mode':parse_mode,'link_preview':False}}
        return self.actions.prepare(payload,preview)

    @_operation
    async def prepare_replace_post_media(self,chat_id,message_id,file_id,caption=None):
        entity,chat=await self._resolve(chat_id)
        message=await self._fetch_message(entity,chat,message_id)
        self._editable(chat,message)
        info=self.files.metadata(file_id)
        if not info['mime_type'].startswith(('image/','video/')):
            raise BackendError('unsupported_media','A post can carry a photo or video.')
        item={'file_id':file_id,'kind':'photo' if info['mime_type'].startswith('image/') else 'video','caption':'','original':False}
        old=getattr(message,'message','') or ''
        now=old if caption is None else _text(caption,1024,'caption',True)
        preview={'action':'replace_post_media','chat':chat,'message_id':message.id,'was_media':media_kind(message),'media':self._media_preview([item]),
                 'caption':{'was':old,'now':now,'kept':caption is None,'diff':word_diff(old,now)},'requires_manage_scope':True,
                 'formatting_note':'Caption formatting is rewritten as plain text.','data_trust':UNTRUSTED}
        payload={'action':'replace_post_media','chat_id':chat['chat_id'],'message_ids':[message.id],'media':item,'text':now,'options':{'parse_mode':'plain','link_preview':False}}
        return self.actions.prepare(payload,preview)

    @_operation
    async def prepare_delete_post(self,chat_id,message_ids):
        entity,chat=await self._channel(chat_id)
        selected=await self._check_messages(entity,chat,message_ids)
        shown=[{'message_id':m.id,'date':getattr(m,'date',None).isoformat() if getattr(m,'date',None) else None,'text':getattr(m,'message','') or '',
                'media':media_kind(m),'views':getattr(m,'views',None)} for m in selected]
        preview={'action':'delete_post','chat':chat,'messages':shown,'destructive':True,'revoke_for_everyone':True,'requires_manage_scope':True,
                 'warning':'Deletion cannot be undone. The complete text of every deleted post is shown.','data_trust':UNTRUSTED}
        payload={'action':'delete_post','chat_id':chat['chat_id'],'message_ids':list(message_ids),'revoke':True}
        return self.actions.prepare(payload,preview)

    @_operation
    async def prepare_pin_post(self,chat_id,message_id,unpin=False):
        entity,chat=await self._channel(chat_id)
        selected=await self._check_messages(entity,chat,[message_id])
        preview={'action':'pin_post','chat':chat,'message_id':message_id,'operation':'unpin' if unpin else 'pin','post':self._source(selected[0],chat),'requires_manage_scope':True,'data_trust':UNTRUSTED}
        payload={'action':'pin_post','chat_id':chat['chat_id'],'message_ids':[message_id],'unpin':bool(unpin)}
        return self.actions.prepare(payload,preview)

    @_operation
    async def get_post_stats(self,chat_id,message_id):
        entity,chat=await self._resolve(chat_id)
        message=await self._fetch_message(entity,chat,message_id)
        reactions=[]
        for item in getattr(getattr(message,'reactions',None),'results',None) or []:
            reaction=getattr(item,'reaction',None)
            reactions.append({'reaction':getattr(reaction,'emoticon',None) or getattr(reaction,'document_id',None) or type(reaction).__name__,'count':getattr(item,'count',0)})
        replies=getattr(getattr(message,'replies',None),'replies',None)
        edited=getattr(message,'edit_date',None)
        return {'chat':chat,'message_id':message.id,'views':getattr(message,'views',None),'forwards':getattr(message,'forwards',None),'reactions':reactions,
                'comments':replies,'pinned':bool(getattr(message,'pinned',False)),'edited_at':edited.isoformat() if edited else None,
                'note':'Telegram reports views and forwards for channel posts and hides reactions or counters it does not expose to this account; null means unavailable.'}
