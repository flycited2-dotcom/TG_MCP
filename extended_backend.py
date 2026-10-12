"""Personal Telegram v2: Saved Messages, paginated reads and reviewed mutations."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from typing import Any

from action_store import ActionStore
from file_store import FileStore
from telegram_backend import TelegramBackend, BackendError, _operation, _chat_id, _limit, _text, _user
from tool_models import ArticleBlock, MediaItem, MessageOptions, ContactChange, GroupChange, StoryOptions


def tl():
    from telethon.tl import functions, types
    from telethon import utils
    return functions,types,utils


def date(value):
    if not value:
        return None
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (ValueError,TypeError):
        raise BackendError('invalid_input','Use an ISO date/time, with timezone for scheduled posts.') from None


def ids(value):
    if isinstance(value,bool) or not isinstance(value,int) or value<=0:
        raise BackendError('invalid_input','Message IDs must be positive integers.')
    return value


def returned_ids(result):
    found=[]
    for update in getattr(result,'updates',[]) or []:
        message=getattr(update,'message',None)
        if message is not None and getattr(message,'id',None):
            found.append(message.id)
        elif type(update).__name__ in ('UpdateMessageID','UpdateStoryID'):
            found.append(update.id)
    if type(result).__name__=='UpdateShortSentMessage':
        found.append(result.id)
    return list(dict.fromkeys(found))


class ExtendedBackend(TelegramBackend):
    def __init__(self,*args,files=None,actions=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.files=files or FileStore(self.output_dir.parent/'staged')
        self.actions=actions or ActionStore(self.drafts_db.parent/'actions.sqlite',self._clock)
        self._known_users={}

    @classmethod
    def from_env(cls):
        instance=super().from_env()
        instance.max_download_bytes=int(os.environ.get('TG_DOWNLOAD_MAX_BYTES',200*1024*1024))
        instance.max_total_download_bytes=int(os.environ.get('TG_DOWNLOAD_TOTAL_BYTES',400*1024*1024))
        return instance

    async def _saved(self):
        client=await self._ready()
        me=await client.get_me()
        _,types,_=tl()
        return types.InputPeerSelf(),{'chat_id':me.id,'title':'Избранное','type':'saved_messages','username':getattr(me,'username',None)}

    async def _resolve(self,chat_id):
        selected=_chat_id(chat_id)
        client=await self._ready()
        me=await client.get_me()
        if selected==me.id:
            return await self._saved()
        if selected in self._known_users:
            entity=self._known_users[selected]
            return entity,{'chat_id':selected,'title':(' '.join([getattr(entity,'first_name','') or '',getattr(entity,'last_name','') or ''])).strip(),
                           'username':getattr(entity,'username',None),'type':'private'}
        return await super()._resolve(selected)

    async def _target(self,chat_id):
        return await self._saved() if chat_id is None else await self._resolve(chat_id)

    async def _story_target(self,chat_id,writing=False):
        entity,chat=await self._target(chat_id)
        if chat['type']=='saved_messages':
            me=await self._client.get_me()
            chat={**chat,'title':'Профиль '+(getattr(me,'first_name','') or str(me.id)),'type':'personal_profile'}
        elif writing and chat['type']=='private':
            raise BackendError('permission_denied','Stories can be published only to your own profile or an eligible channel.')
        return entity,chat

    def _link(self,chat,message_id):
        if chat.get('type')=='saved_messages':
            return None
        if chat.get('username') and chat.get('type')!='private':
            return f"https://t.me/{chat['username']}/{message_id}"
        if chat['chat_id']<-1000000000000:
            return f"https://t.me/c/{-chat['chat_id']-1000000000000}/{message_id}"
        return None

    def _pack(self,message,chat):
        result=self._message(message,chat['chat_id'])
        result['message_url']=self._link(chat,message.id)
        return result

    @_operation
    async def get_saved_messages(self,limit=50,offset_id=0,query='',saved_dialog_id=None):
        entity,metadata=await self._saved()
        return await self._history(entity,metadata,limit,offset_id,query,saved_dialog_id=saved_dialog_id)

    @_operation
    async def get_saved_chat(self):
        _,metadata=await self._saved()
        return {'chat':metadata,'resolved_by':'authenticated_self_id','name_search_used':False}

    @_operation
    async def get_saved_sections(self,limit=30,offset_id=0,offset_date=None,offset_peer_id=None):
        f,t,u=tl()
        await self._ready()
        offset_peer=t.InputPeerEmpty() if offset_peer_id is None else await self._client.get_input_entity(offset_peer_id)
        result=await self._client(f.messages.GetSavedDialogsRequest(offset_date=date(offset_date),offset_id=offset_id,
                       offset_peer=offset_peer,limit=_limit(limit,100),hash=0))
        dialogs=[{'peer_id':u.get_peer_id(d.peer),'top_message_id':getattr(d,'top_message',None),'pinned':bool(getattr(d,'pinned',False))} for d in getattr(result,'dialogs',[])]
        tags=await self._client(f.messages.GetSavedReactionTagsRequest(hash=0))
        return {'sections':dialogs,'tags':[{'title':getattr(tag,'title',None),'count':tag.count,'reaction':tag.reaction.to_dict()} for tag in getattr(tags,'tags',[])],
                'pagination':'Pass last section peer_id/top_message_id and its message date to request the next page. Tags require Telegram Premium.'}

    async def _history(self,entity,chat,limit,offset_id=0,query='',sender_id=None,since=None,until=None,media_type=None,topic_id=None,saved_dialog_id=None):
        limit=_limit(limit,100)
        if isinstance(offset_id,bool) or not isinstance(offset_id,int) or offset_id<0:
            raise BackendError('invalid_input','offset_id must be a nonnegative message ID.')
        f,t,_=tl()
        if since or sender_id or media_type or topic_id or saved_dialog_id is not None:
            filters={'photo':t.InputMessagesFilterPhotos,'video':t.InputMessagesFilterVideo,'voice':t.InputMessagesFilterVoice,
                     'audio':t.InputMessagesFilterMusic,'document':t.InputMessagesFilterDocument,'animation':t.InputMessagesFilterGif,
                     'video_note':t.InputMessagesFilterRoundVideo,'url':t.InputMessagesFilterUrl}
            if media_type and media_type not in filters:
                raise BackendError('invalid_input','Unknown media_type filter.')
            result=await self._client(f.messages.SearchRequest(peer=await self._client.get_input_entity(entity),q=query,
                filter=filters.get(media_type,t.InputMessagesFilterEmpty)(),min_date=date(since),max_date=date(until),
                offset_id=offset_id,add_offset=0,limit=limit+1,max_id=0,min_id=0,hash=0,
                from_id=await self._client.get_input_entity(sender_id) if sender_id else None,
                saved_peer_id=await self._client.get_input_entity(saved_dialog_id) if saved_dialog_id is not None else None,top_msg_id=topic_id))
            messages=list(getattr(result,'messages',[]))
        else:
            kwargs={'limit':limit+1,'offset_id':offset_id}
            if query:
                kwargs['search']=_text(query,1000,'query')
            if until:
                kwargs['offset_date']=date(until)
            messages=[m async for m in self._client.iter_messages(entity,**kwargs)]
        result=[]
        for message in messages[:limit]:
            self._verify_message_chat(message,chat['chat_id'])
            result.append(self._pack(message,chat))
        return {'chat':chat,'messages':result,'has_more':len(messages)>limit,
                'next_offset_id':result[-1]['message_id'] if result and len(messages)>limit else None}

    @_operation
    async def read_history(self,chat_id,limit=50,offset_id=0,topic_id=None):
        entity,metadata=await self._resolve(chat_id)
        return await self._history(entity,metadata,limit,offset_id,topic_id=topic_id)

    @_operation
    async def search_history(self,chat_id,query='',limit=50,offset_id=0,sender_id=None,since=None,until=None,media_type=None,topic_id=None):
        entity,metadata=await self._resolve(chat_id)
        return await self._history(entity,metadata,limit,offset_id,query,sender_id,since,until,media_type,topic_id)

    @_operation
    async def find_users(self,query,limit=20):
        query=_text(query,200,'query').strip()
        f,t,_=tl()
        client=await self._ready()
        result=await client(f.contacts.GetContactsRequest(hash=0))
        phone=re.sub(r'[^0-9]','',query) if re.fullmatch(r'[+0-9() -]+',query) else None
        matches=[]
        for user in getattr(result,'users',[]):
            name=' '.join([getattr(user,'first_name','') or '',getattr(user,'last_name','') or '',getattr(user,'username','') or ''])
            if (phone and phone==getattr(user,'phone',None)) or (not phone and query.lstrip('@').casefold() in name.casefold()):
                matches.append(user)
        if not matches and (phone or re.fullmatch(r'@?[a-zA-Z][a-zA-Z0-9_]{3,31}',query)):
            try:
                if phone:
                    resolved=await client(f.contacts.ResolvePhoneRequest(phone=phone))
                    matches=list(getattr(resolved,'users',[]))
                else:
                    user=await client.get_entity(query)
                    if type(user).__name__=='User':
                        matches=[user]
            except Exception as error:
                if type(error).__name__ in ('PhoneNotOccupiedError','UsernameNotOccupiedError','UsernameInvalidError') or isinstance(error,ValueError):
                    raise BackendError('user_not_accessible','No user is visible for this number or username. Privacy restrictions are respected.') from None
                raise
        for user in matches[:_limit(limit,50)]:
            self._known_users[user.id]=user
        return {'users':[_user(u,include_phone=True) for u in matches[:limit]],'privacy':'Only fields Telegram makes available to this account.'}

    @_operation
    async def get_chat(self,chat_id):
        entity,metadata=await self._resolve(chat_id)
        details=dict(metadata)
        for key in ('creator','megagroup','broadcast','forum','restricted','noforwards','participants_count'):
            details[key]=getattr(entity,key,None)
        f,t,u=tl()
        if type(entity).__name__ in ('Channel','Chat'):
            full=await self._client(f.channels.GetFullChannelRequest(channel=entity)) if type(entity).__name__=='Channel' else await self._client(f.messages.GetFullChatRequest(chat_id=entity.id))
            info=getattr(full,'full_chat',None)
            details['description']=getattr(info,'about',None)
            details['participants_count']=getattr(info,'participants_count',details['participants_count'])
            linked=getattr(info,'linked_chat_id',None)
            details['discussion_chat_id']=-(1000000000000+linked) if linked else None
        try:
            permissions=await self._client.get_permissions(entity,'me')
            details['permissions']={key:bool(getattr(permissions,key,False)) for key in ('is_creator','is_admin','is_banned','send_messages','send_media','pin_messages','invite_users','change_info','delete_messages')}
        except Exception as error:
            if type(error).__name__ not in ('ChatAdminRequiredError','UserNotParticipantError') and not isinstance(error,TypeError):
                raise
            details['permissions_unavailable']=True
        return {'chat':details}

    @_operation
    async def get_topics(self,chat_id,query='',limit=30,offset_id=0,offset_topic=0,offset_date=None):
        f,t,u=tl()
        entity,chat=await self._resolve(chat_id)
        if not getattr(entity,'forum',False):
            raise BackendError('topics_unavailable','The selected chat is not an accessible Telegram forum.')
        result=await self._client(f.messages.GetForumTopicsRequest(peer=await self._client.get_input_entity(entity),q=query or None,
                    offset_date=date(offset_date),offset_id=offset_id,offset_topic=offset_topic,limit=_limit(limit,100)))
        topics=[{'topic_id':topic.id,'title':getattr(topic,'title',None),'closed':getattr(topic,'closed',False),
                 'top_message_id':getattr(topic,'top_message',None),'date':getattr(topic,'date',None).isoformat() if getattr(topic,'date',None) else None} for topic in getattr(result,'topics',[])]
        messages={m.id:m for m in getattr(result,'messages',[])}
        cursor=None
        if topics:
            last=topics[-1]
            message=messages.get(last['top_message_id'])
            when=last['date'] if getattr(result,'order_by_create_date',False) else getattr(message,'date',None).isoformat() if getattr(message,'date',None) else last['date']
            cursor={'offset_topic':last['topic_id'],'offset_id':last['top_message_id'],'offset_date':when}
        return {'chat':chat,'topics':topics,'next_cursor':cursor}

    @_operation
    async def get_album(self,chat_id,message_id,limit=20):
        entity,chat=await self._resolve(chat_id)
        message=await self._client.get_messages(entity,ids=ids(message_id))
        if message is None:
            raise BackendError('message_not_found','The selected message is unavailable.')
        self._verify_message_chat(message,chat['chat_id'])
        grouped=getattr(message,'grouped_id',None)
        nearby=await self._client.get_messages(entity,ids=list(range(max(1,message_id-20),message_id+21))) if grouped else [message]
        found=[]
        for item in nearby:
            if item is not None and (not grouped or getattr(item,'grouped_id',None)==grouped):
                self._verify_message_chat(item,chat['chat_id'])
                found.append(self._pack(item,chat))
        return {'chat':chat,'album_id':grouped,'messages':sorted(found,key=lambda m:m['message_id'])[:_limit(limit,100)]}

    async def attachment(self,chat_id,message_id):
        result=await self.download_attachment(chat_id,message_id)
        try:
            staged=await self.files.import_path(result['path'],result['filename'],result['mime_type'])
        finally:
            Path(result['path']).unlink(missing_ok=True)
        return {**staged,'chat':result['chat'],'message_id':message_id}

    async def _check_messages(self,entity,chat,message_ids):
        if not 1<=len(message_ids)<=100:
            raise BackendError('invalid_input','Select between 1 and 100 message IDs.')
        result=await self._client.get_messages(entity,ids=[ids(x) for x in message_ids])
        if not isinstance(result,(list,tuple)):
            result=[result]
        for message in result:
            if message is None:
                raise BackendError('message_not_found','One of the selected messages is unavailable.')
            self._verify_message_chat(message,chat['chat_id'])
        return result

    def _media_preview(self,items):
        result=[]
        for item in items:
            info=self.files.metadata(item['file_id'])
            self.files.verified_path(item['file_id'])
            result.append({**item,**{k:info[k] for k in ('filename','mime_type','size_bytes','sha256')}})
        return result

    def _options(self,options):
        result=MessageOptions.model_validate(options or {}).model_dump()
        when=date(result['schedule_at'])
        if result['schedule_at'] and not re.search(r'(?:Z|[+-]\d\d:\d\d)$',result['schedule_at']):
            raise BackendError('invalid_input','Scheduled publication requires an explicit ISO timezone, e.g. +03:00.')
        if when and when<=datetime.now(timezone.utc):
            raise BackendError('invalid_input','Scheduled publication must be in the future.')
        return result

    @_operation
    async def prepare_send(self,chat_id,text='',media=None,options=None):
        entity,chat=await self._target(chat_id)
        options=self._options(options)
        media=[MediaItem.model_validate(item).model_dump() for item in (media or [])]
        if len(media)>10:
            raise BackendError('invalid_input','An album may contain at most 10 items.')
        _text(text,4096 if not media else 1024,'text',allow_empty=bool(media))
        if len(media)>1:
            groups={'photo':'visual','video':'visual','document':'document','audio':'audio'}
            kinds={groups.get(item['kind'],'unsupported') for item in media}
            if len(kinds)!=1 or 'unsupported' in kinds or (kinds=={'visual'} and any(item['original'] for item in media)):
                raise BackendError('unsupported_album','Use all photos/videos with original=false, all documents, or all audio. Voice notes/stickers/video notes cannot form an album.')
        if options['reply_to']:
            await self._check_messages(entity,chat,[options['reply_to']])
        preview={'action':'send_media' if media else 'send_message','recipient':chat,'text':text,'media':self._media_preview(media),'options':options}
        payload={**preview,'chat_id':chat['chat_id'],'random_id':secrets.randbits(62)+1}
        return self.actions.prepare(payload,preview)

    @_operation
    async def prepare_message_action(self,action,chat_id,message_ids=None,text='',emoji=None,custom_emoji_id=None,target_chat_id=None,options=None,revoke=True):
        if action not in ('reply','forward','edit','delete','react','pin','unpin'):
            raise BackendError('invalid_input','Unsupported message action.')
        entity,chat=await self._resolve(chat_id)
        message_ids=message_ids or []
        selected=await self._check_messages(entity,chat,message_ids)
        options=self._options(options)
        preview={'action':action,'source_chat':chat,'message_ids':message_ids,'selected_messages':[self._pack(m,chat) for m in selected]}
        payload={'action':action,'chat_id':chat['chat_id'],'message_ids':message_ids,'options':options,'revoke':bool(revoke),'random_ids':[secrets.randbits(63) for _ in message_ids]}
        if action in ('reply','edit'):
            if len(message_ids)!=1:
                raise BackendError('invalid_input','Reply/edit requires one message.')
            payload['text']=preview['text']=_text(text,4096,'text')
        if action=='forward':
            _,target=await self._target(target_chat_id)
            payload['target_chat_id']=target['chat_id']
            preview['recipient']=target
        if action=='react':
            if len(message_ids)!=1 or (emoji is not None and custom_emoji_id is not None):
                raise BackendError('invalid_input','Choose one message and either emoji or custom_emoji_id.')
            if emoji is not None:
                _text(emoji,16,'emoji',allow_empty=True)
            payload['emoji']=preview['emoji']=emoji
            payload['custom_emoji_id']=preview['custom_emoji_id']=custom_emoji_id
        if action in ('delete','unpin'):
            preview['destructive']=True
            preview['revoke_for_everyone']=bool(revoke)
        return self.actions.prepare(payload,preview)

    @_operation
    async def create_article(self,title,blocks,chat_id=None):
        _,chat=await self._target(chat_id)
        title=_text(title,256,'title')
        blocks=[ArticleBlock.model_validate(item).model_dump() for item in blocks]
        if not 1<=len(blocks)<=200 or len(title)+sum(len(b['text'])+sum(len(r['text']) for r in b['runs'])+sum(map(len,b['items'])) for b in blocks)>32000:
            raise BackendError('invalid_input','A native article supports 1..200 blocks and at most 32000 text characters here.')
        media=[]
        for block in blocks:
            if block['type']=='photo':
                if not block['file_id']:
                    raise BackendError('invalid_input','A photo block requires the original staged file_id.')
                info=self.files.metadata(block['file_id'])
                if info['mime_type'] not in ('image/jpeg','image/png','image/webp'):
                    raise BackendError('unsupported_image','Native article photos require JPEG, PNG or WebP.')
                media.append({'file_id':block['file_id'],'kind':'photo','caption':block['text'],'original':False})
            for run in block['runs']:
                if run['url'] and not re.match(r'^https?://[^\s]+$',run['url']):
                    raise BackendError('invalid_input','Article hyperlinks must use HTTP(S).')
        preview={'action':'publish_article','recipient':chat,'title':title,'blocks':blocks,'media':self._media_preview(media),
                 'publication_type':'native_telegram_rich_message','single_message':True,'limitations':'Requires Telegram client/API support and account eligibility; Telegram may compress inline photos.'}
        return self.actions.prepare({'action':'publish_article','chat_id':chat['chat_id'],'title':title,'blocks':blocks,'media':media,'random_id':secrets.randbits(63)},preview)

    @_operation
    async def prepare_story(self,file_id,caption='',chat_id=None,options=None):
        _,chat=await self._story_target(chat_id,True)
        info=self.files.metadata(file_id)
        if not (info['mime_type'].startswith('image/') or info['mime_type'].startswith('video/')):
            raise BackendError('unsupported_media','Stories require a photo or supported video.')
        options=StoryOptions.model_validate(options or {}).model_dump()
        for link in options['links']:
            if not re.match(r'^https://[^\s]+$',link['url']):
                raise BackendError('invalid_input','Story link areas require HTTPS URLs.')
        if chat['type']=='channel' and options['audience']!='everyone':
            raise BackendError('unsupported_privacy','Channel Stories require their channel audience; explicitly select everyone rather than implying contact-only visibility.')
        if options['audience']=='selected' and not options['selected_user_ids']:
            raise BackendError('invalid_input','Specify selected recipients for a selected-audience Story.')
        media={'file_id':file_id,'kind':'photo' if info['mime_type'].startswith('image/') else 'video','caption':_text(caption,1024,'caption',True),'original':False}
        preview={'action':'publish_story','recipient':chat,'caption':caption,'options':options,'media':self._media_preview([media]),'publication_type':'profile_or_channel_story'}
        return self.actions.prepare({'action':'publish_story','chat_id':chat['chat_id'],'media':[media],'options':options,'random_id':secrets.randbits(63)},preview)

    @_operation
    async def get_stories(self,chat_id=None,archive=False,offset_id=0,limit=30):
        f,t,u=tl()
        entity,chat=await self._story_target(chat_id)
        peer=await self._client.get_input_entity(entity)
        result=await self._client(f.stories.GetStoriesArchiveRequest(peer=peer,offset_id=offset_id,limit=_limit(limit,100))) if archive else await self._client(f.stories.GetPeerStoriesRequest(peer=peer))
        entries=getattr(getattr(result,'stories',None),'stories',None)
        if entries is None:
            entries=getattr(result,'stories',[])
        stories=[{'story_id':s.id,'date':getattr(s,'date',None).isoformat() if getattr(s,'date',None) else None,
                  'expire_date':getattr(s,'expire_date',None).isoformat() if getattr(s,'expire_date',None) else None,'caption':getattr(s,'caption',None),
                  'views':getattr(getattr(s,'views',None),'views_count',None),'public':getattr(s,'public',False)} for s in entries]
        return {'chat':chat,'stories':stories,'next_offset_id':stories[-1]['story_id'] if stories else None}

    @_operation
    async def prepare_story_delete(self,story_ids,chat_id=None):
        _,chat=await self._story_target(chat_id,True)
        if not 1<=len(story_ids)<=100:
            raise BackendError('invalid_input','Select 1..100 Story IDs.')
        story_ids=[ids(v) for v in story_ids]
        payload={'action':'delete_story','chat_id':chat['chat_id'],'story_ids':story_ids}
        return self.actions.prepare(payload,{'action':'delete_story','recipient':chat,'story_ids':story_ids,'destructive':True})

    @_operation
    async def prepare_story_edit(self,story_id,caption,chat_id=None,file_id=None):
        f,t,u=tl()
        entity,chat=await self._story_target(chat_id,True)
        result=await self._client(f.stories.GetStoriesByIDRequest(peer=await self._client.get_input_entity(entity),id=[ids(story_id)]))
        available=getattr(result,'stories',[])
        if not available or type(available[0]).__name__!='StoryItem':
            raise BackendError('story_not_found','The selected Story is not available for editing.')
        media=[]
        if file_id:
            info=self.files.metadata(file_id)
            if not info['mime_type'].startswith(('image/','video/')):
                raise BackendError('unsupported_media','Story replacement requires an image or video.')
            media=[{'file_id':file_id,'kind':'photo' if info['mime_type'].startswith('image/') else 'video','original':False}]
        preview={'action':'edit_story','recipient':chat,'story_id':story_id,'original_caption':getattr(available[0],'caption',None),
                 'caption':_text(caption,1024,'caption',True),'media':self._media_preview(media)}
        return self.actions.prepare({'action':'edit_story','chat_id':chat['chat_id'],'story_id':story_id,'caption':caption,'media':media},preview)

    @_operation
    async def prepare_contact(self,change):
        change=ContactChange.model_validate(change).model_dump()
        await self._ready()
        if change['action'] in ('add','update') and (not change['first_name'].strip() or not re.fullmatch(r'\+?[0-9]{5,20}',change['phone'])):
            raise BackendError('invalid_input','A first name and accessible international phone number are required.')
        if change['action']=='delete' and not change['user_id']:
            raise BackendError('invalid_input','Select the contact user_id.')
        if change['action']=='update' and not change['user_id']:
            raise BackendError('invalid_input','Updating a contact requires its visible user_id; importing a different phone is a separate action.')
        return self.actions.prepare({'action':'manage_contact','change':change},{'action':'manage_contact','change':change,'requires_manage_scope':True})

    @_operation
    async def prepare_group(self,change,chat_id=None):
        change=GroupChange.model_validate(change).model_dump()
        creating=change['action'] in ('create_group','create_channel')
        if creating and not change['title'].strip():
            raise BackendError('invalid_input','A new group/channel requires a title.')
        if change['action']=='create_group' and not change['user_ids']:
            raise BackendError('invalid_input','A basic group requires explicitly selected initial members.')
        if change['action'] in ('remove','set_admin','ban','unban') and not change['user_id']:
            raise BackendError('invalid_input','Select one user_id.')
        if change['action']=='invite' and not change['user_ids']:
            raise BackendError('invalid_input','Select users to invite; Telegram privacy limits apply.')
        if not creating and chat_id is None:
            raise BackendError('invalid_input','Choose the group/channel chat_id.')
        chat=None
        if not creating:
            entity,chat=await self._resolve(chat_id)
            if chat['type'] not in ('group','channel'):
                raise BackendError('invalid_input','Select a group or channel for this management action.')
            if change['action']=='set_admin' and type(entity).__name__=='Chat' and change['admin_rights']:
                raise BackendError('unsupported_operation','Basic groups do not support granular admin rights; use an eligible supergroup instead of silently granting full administration.')
        return self.actions.prepare({'action':'manage_group','chat_id':chat['chat_id'] if chat else None,'change':change},
                                   {'action':'manage_group','recipient':chat,'change':change,'requires_manage_scope':True,'destructive':change['action'] in ('remove','ban','set_admin')})

    def required_scope(self,draft_id):
        row=self.actions.inspect(draft_id)
        payload=json.loads(row['payload']) if row['payload'] else {}
        # Consumed actions retain the preview, allowing safe scope checks on repeats.
        action=payload.get('action') or json.loads(row['preview']).get('action')
        return 'telegram.manage' if action in ('manage_contact','manage_group') else 'telegram.send'

    def article_publication_evidence(self,account_id=None):
        """Only a persisted successful publish AND read-back is live acceptance."""
        business=self.output_dir.parent/'verification'/'business-article-results.json'
        try:
            record=json.loads(business.read_text(encoding='utf-8'))
            result=record['result']
            chat=result['chat']
            files=record['files']
            photo_ids=[str(b['photo_id']) for b in record['article_blocks'] if b['_']=='PageBlockPhoto']
            verified=(record['status']=='sent_and_verified' and record['native_article_verified'] is True
                      and record['read_back_verified'] is True and record['messages_sent']==1
                      and record['chatgpt_six_original_file_params_verified'] is True
                      and record['source_photo_order_verified'] is True
                      and len(files)==6 and len({f['sha256'] for f in files})==6
                      and len(photo_ids)==6 and len(set(photo_ids))==6
                      and result['sent'] is True and result['publication_type']=='native_article'
                      and chat['type']=='saved_messages' and (account_id is None or chat['chat_id']==account_id)
                      and len(result['message_ids'])==1 and type(result['message_ids'][0]) is int
                      and result['message_ids'][0]>0)
            if verified:
                return {'verified':True,'message_ids':result['message_ids'],
                        'scenario':'one_native_article_with_six_original_chatgpt_photos',
                        'six_original_photos_from_chatgpt_verified':True}
        except (OSError,ValueError,KeyError,TypeError):
            pass
        path=self.output_dir.parent/'verification'/'article-retry-results.json'
        try:
            record=json.loads(path.read_text(encoding='utf-8'))
            result=record['result']
            chat=result['chat']
            verified=(record['status']=='sent_and_verified' and record['native_article_verified'] is True
                      and record['read_back_verified'] is True and record['messages_sent']==1
                      and result['sent'] is True and result['publication_type']=='native_article'
                      and chat['type']=='saved_messages' and (account_id is None or chat['chat_id']==account_id)
                      and len(result['message_ids'])==1 and type(result['message_ids'][0]) is int
                      and result['message_ids'][0]>0)
            if verified:
                return {'verified':True,'message_ids':result['message_ids'],
                        'scenario':'one_native_article_with_one_inline_photo',
                        'six_original_photos_from_chatgpt_verified':False}
        except (OSError,ValueError,KeyError,TypeError):
            pass
        return {'verified':False,'scenario':'live_publication_not_verified',
                'six_original_photos_from_chatgpt_verified':False}

    def preview_action(self,draft_id):
        row=self.actions.inspect(draft_id)
        return {'draft_id':draft_id,'confirmation_hash':row['digest'],'preview':json.loads(row['preview']),'state':row['state'],'expires_at':row['expires']}

    async def _media(self,item,peer):
        f,t,u=tl()
        info=self.files.metadata(item['file_id'])
        path=self.files.verified_path(item['file_id'])
        uploaded=await self._client.upload_file(path,file_name=info['filename'])
        if item['kind']=='photo' and not item['original']:
            return t.InputMediaUploadedPhoto(file=uploaded)
        attributes=[t.DocumentAttributeFilename(info['filename'])]
        kind=item['kind']
        if kind in ('video','video_note','animation','voice','audio'):
            from media_processing import probe_media
            meta=await asyncio.to_thread(probe_media,path)
            if kind in ('video','video_note','animation'):
                attributes.append(t.DocumentAttributeVideo(duration=meta.get('duration_seconds',0),w=meta.get('width',0),h=meta.get('height',0),round_message=kind=='video_note',supports_streaming=kind!='animation'))
                if kind=='animation':
                    attributes.append(t.DocumentAttributeAnimated())
            else:
                attributes.append(t.DocumentAttributeAudio(duration=int(meta.get('duration_seconds',0)),voice=kind=='voice',title=info['filename'] if kind=='audio' else None))
        if kind=='sticker':
            attributes.append(t.DocumentAttributeSticker(alt='',stickerset=t.InputStickerSetEmpty()))
            if info['mime_type'] in ('image/webp','image/png'):
                from PIL import Image
                with Image.open(path) as image:
                    w,h=image.size
                attributes.append(t.DocumentAttributeImageSize(w,h))
            elif info['mime_type']=='application/x-tgsticker':
                import gzip
                with gzip.open(path,'rb') as stream:
                    data=stream.read(2*1024*1024+1)
                if len(data)>2*1024*1024:
                    raise BackendError('unsupported_media','Animated sticker data exceeds supported limits.')
                sticker=json.loads(data)
                attributes.extend([t.DocumentAttributeImageSize(int(sticker.get('w',512)),int(sticker.get('h',512))),t.DocumentAttributeAnimated()])
            elif info['mime_type']=='video/webm':
                from media_processing import probe_media
                meta=await asyncio.to_thread(probe_media,path)
                attributes.append(t.DocumentAttributeVideo(duration=meta.get('duration_seconds',0),w=meta.get('width',0),h=meta.get('height',0)))
            else:
                raise BackendError('unsupported_media','Sticker bytes must use a supported WebP/PNG/TGS/WebM format.')
        return t.InputMediaUploadedDocument(file=uploaded,mime_type=info['mime_type'],attributes=attributes,
                                           force_file=kind=='document' or (kind=='photo' and item['original']),nosound_video=kind=='animation')

    def _parsed(self,text,mode):
        if mode=='html':
            from telethon.extensions import html
            return html.parse(text)
        if mode=='markdown':
            from telethon.extensions import markdown
            return markdown.parse(text)
        return text,[]

    def _reply(self,options):
        _,t,_=tl()
        return t.InputReplyToMessage(reply_to_msg_id=options['reply_to'] or options['topic_id'],top_msg_id=options['topic_id']) if options['reply_to'] or options['topic_id'] else None

    def _rich_text(self,block):
        _,t,_=tl()
        if not block['runs']:
            return t.TextPlain(block['text']) if block['text'] else t.TextEmpty()
        result=[]
        styles={'bold':t.TextBold,'italic':t.TextItalic,'code':t.TextFixed,'strike':t.TextStrike,'underline':t.TextUnderline}
        for run in block['runs']:
            node=t.TextPlain(run['text'])
            if run['style'] in styles:
                node=styles[run['style']](node)
            if run['url']:
                node=t.TextUrl(node,run['url'],webpage_id=0)
            result.append(node)
        return t.TextConcat(result)

    async def _article(self,payload,peer):
        f,t,u=tl()
        photos=[]
        # Rich-message headings differ from legacy Instant View title/header
        # blocks. Match the official iv_rich_message_serializer.cpp contract.
        blocks=[t.PageBlockHeading1(t.TextPlain(payload['title']))]
        for block in payload['blocks']:
            text=self._rich_text(block)
            if block['type']=='photo':
                media=await self._media({'file_id':block['file_id'],'kind':'photo','original':False},peer)
                stored=await self._client(f.messages.UploadMediaRequest(peer=peer,media=media))
                photo=u.get_input_photo(stored.photo)
                photos.append(photo)
                blocks.append(t.PageBlockPhoto(photo_id=photo.id,caption=t.PageCaption(text,t.TextEmpty())))
            elif block['type']=='heading':
                heading={1:t.PageBlockHeading1,2:t.PageBlockHeading2,3:t.PageBlockHeading3}[block['level']]
                blocks.append(heading(text))
            elif block['type']=='list':
                blocks.append(t.PageBlockList([t.PageListItemText(t.TextPlain(item)) for item in block['items']]))
            elif block['type']=='quote':
                blocks.append(t.PageBlockBlockquote(text,t.TextEmpty()))
            elif block['type']=='divider':
                blocks.append(t.PageBlockDivider())
            else:
                blocks.append(t.PageBlockParagraph(text))
        request=f.messages.SendMessageRequest(peer=peer,message='',random_id=payload['random_id'],
                        rich_message=t.InputRichMessage(blocks=blocks,photos=photos or None))
        return await self._client(request)

    async def _group(self,payload):
        f,t,u=tl()
        change=payload['change']
        action=change['action']
        entity,chat=await self._resolve(payload['chat_id']) if payload['chat_id'] is not None else (None,None)
        user=await self._client.get_input_entity(change['user_id']) if change['user_id'] else None
        users=[await self._client.get_input_entity(value) for value in change['user_ids']]
        if action=='create_group':
            result=await self._client(f.messages.CreateChatRequest(users=[u.get_input_user(x) for x in users],title=change['title']))
        elif action=='create_channel':
            result=await self._client(f.channels.CreateChannelRequest(title=change['title'],about=change['description'],broadcast=True))
        elif action=='set_title':
            result=await self._client(f.channels.EditTitleRequest(channel=entity,title=change['title'])) if type(entity).__name__=='Channel' else await self._client(f.messages.EditChatTitleRequest(chat_id=entity.id,title=change['title']))
        elif action=='set_description':
            result=await self._client(f.messages.EditChatAboutRequest(peer=entity,about=change['description']))
        elif action=='invite':
            if type(entity).__name__=='Channel':
                result=await self._client(f.channels.InviteToChannelRequest(channel=entity,users=[u.get_input_user(x) for x in users]))
            else:
                if len(users)!=1:
                    raise BackendError('unsupported_operation','Basic groups accept one invited user per reviewed action.')
                result=await self._client(f.messages.AddChatUserRequest(chat_id=entity.id,user_id=u.get_input_user(users[0]),fwd_limit=0))
        elif action=='remove':
            result=await self._client.kick_participant(entity,user)
        elif action=='set_admin':
            supported=['change_info','post_messages','edit_messages','delete_messages','ban_users','invite_users','pin_messages','add_admins','manage_call','manage_topics']
            if type(entity).__name__=='Channel':
                rights=t.ChatAdminRights(**{key:key in change['admin_rights'] for key in supported})
                result=await self._client(f.channels.EditAdminRequest(channel=u.get_input_channel(await self._client.get_input_entity(entity)),user_id=u.get_input_user(user),admin_rights=rights,rank=''))
            else:
                result=await self._client(f.messages.EditChatAdminRequest(chat_id=entity.id,user_id=u.get_input_user(user),is_admin=False))
        elif action in ('ban','unban'):
            result=await self._client.edit_permissions(entity,user,view_messages=action=='unban')
        elif action=='create_topic':
            result=await self._client(f.messages.CreateForumTopicRequest(peer=entity,title=change['title'],random_id=secrets.randbits(63)))
        else:
            raise BackendError('unsupported_operation','Unsupported management action.')
        created=[{'chat_id':u.get_peer_id(c),'title':getattr(c,'title',None)} for c in getattr(result,'chats',[])]
        return {'changed':True,'action':action,'chats':created,'message_ids':returned_ids(result)}

    @_operation
    async def execute_action(self,draft_id,confirmation_hash):
        if not self.allow_send:
            raise BackendError('send_disabled','Outbound actions are disabled on this server.')
        payload,prior=self.actions.claim(draft_id,confirmation_hash)
        if prior is not None:
            return prior
        try:
            await self._ready()
            result=await self._execute(payload)
            result.update({'draft_id':draft_id,'executed':True,'already_executed':False})
            self.actions.finish(draft_id,result)
            return result
        except BaseException:
            self.actions.finish(draft_id)
            raise

    async def _execute(self,p):
        f,t,u=tl()
        action=p['action']
        if action=='manage_group':
            return await self._group(p)
        if action=='manage_contact':
            c=p['change']
            if c['action']=='delete':
                result=await self._client(f.contacts.DeleteContactsRequest(id=[u.get_input_user(await self._client.get_input_entity(c['user_id']))]))
                return {'changed':True,'action':'delete_contact'}
            if c['action']=='update':
                await self._client(f.contacts.AddContactRequest(id=u.get_input_user(await self._client.get_input_entity(c['user_id'])),first_name=c['first_name'],last_name=c['last_name'],phone=c['phone'],add_phone_privacy_exception=False))
                return {'changed':True,'action':'update_contact','user_id':c['user_id']}
            result=await self._client(f.contacts.ImportContactsRequest(contacts=[t.InputPhoneContact(client_id=secrets.randbits(63),phone=c['phone'],first_name=c['first_name'],last_name=c['last_name'])]))
            if not getattr(result,'imported',[]):
                raise BackendError('contact_not_accessible','Telegram did not import this contact; privacy limits or retry restrictions apply.')
            return {'changed':True,'contacts':[_user(user,include_phone=True) for user in getattr(result,'users',[])]}
        entity,chat=await self._story_target(p['chat_id'],True) if action in ('publish_story','delete_story','edit_story') else await self._resolve(p['chat_id'])
        peer=await self._client.get_input_entity(entity)
        options=p.get('options',{})
        if action in ('send_message','reply'):
            text,entities=self._parsed(p['text'],options['parse_mode'])
            if action=='reply':
                options['reply_to']=p['message_ids'][0]
            request=f.messages.SendMessageRequest(peer=peer,message=text,entities=entities,random_id=p.get('random_id',p.get('random_ids',[0])[0]),
                reply_to=self._reply(options),silent=options['silent'],schedule_date=date(options['schedule_at']),no_webpage=not options['link_preview'])
            result=await self._client(request)
        elif action=='send_media':
            media=[]
            for index,item in enumerate(p['media']):
                uploaded=await self._media(item,peer)
                caption,entities=self._parsed(item['caption'] or (p['text'] if index==0 else ''),options['parse_mode'])
                if len(p['media'])==1:
                    request=f.messages.SendMediaRequest(peer=peer,media=uploaded,message=caption,entities=entities,
                        random_id=p['random_id'],reply_to=self._reply(options),silent=options['silent'],schedule_date=date(options['schedule_at']))
                    result=await self._client(request)
                else:
                    stored=await self._client(f.messages.UploadMediaRequest(peer=peer,media=uploaded))
                    uploaded=t.InputMediaPhoto(u.get_input_photo(stored.photo)) if getattr(stored,'photo',None) else t.InputMediaDocument(u.get_input_document(stored.document))
                    media.append(t.InputSingleMedia(media=uploaded,random_id=p['random_id']+index,message=caption,entities=entities))
            if media:
                result=await self._client(f.messages.SendMultiMediaRequest(peer=peer,multi_media=media,reply_to=self._reply(options),silent=options['silent'],schedule_date=date(options['schedule_at'])))
        elif action=='forward':
            target,target_chat=await self._resolve(p['target_chat_id'])
            result=await self._client(f.messages.ForwardMessagesRequest(from_peer=peer,id=p['message_ids'],random_id=p['random_ids'],to_peer=await self._client.get_input_entity(target),
                  schedule_date=date(options['schedule_at']),silent=options['silent']))
            chat=target_chat
        elif action=='edit':
            text,entities=self._parsed(p['text'],options['parse_mode'])
            result=await self._client(f.messages.EditMessageRequest(peer=peer,id=p['message_ids'][0],message=text,entities=entities,no_webpage=not options['link_preview']))
            return {'edited':True,'chat':chat,'message_ids':p['message_ids']}
        elif action=='delete':
            await self._client.delete_messages(entity,p['message_ids'],revoke=p['revoke'])
            return {'deleted':True,'chat':chat,'message_ids':p['message_ids'],'revoke':p['revoke']}
        elif action in ('pin','unpin'):
            result=await self._client.pin_message(entity,p['message_ids'][0],notify=False) if action=='pin' else await self._client.unpin_message(entity,p['message_ids'][0])
            return {'changed':True,'action':action,'chat':chat,'message_ids':p['message_ids']}
        elif action=='react':
            reactions=[t.ReactionCustomEmoji(p['custom_emoji_id'])] if p['custom_emoji_id'] else [t.ReactionEmoji(p['emoji'])] if p['emoji'] else []
            result=await self._client(f.messages.SendReactionRequest(peer=peer,msg_id=p['message_ids'][0],reaction=reactions))
            return {'reacted':True,'chat':chat,'message_ids':p['message_ids'],'emoji':p['emoji'],'custom_emoji_id':p['custom_emoji_id']}
        elif action=='publish_article':
            result=await self._article(p,peer)
        elif action=='publish_story':
            opts=p['options']
            audience=opts['audience']
            privacy=[t.InputPrivacyValueAllowAll()] if audience=='everyone' else [t.InputPrivacyValueAllowContacts()] if audience=='contacts' else [t.InputPrivacyValueAllowCloseFriends()] if audience=='close_friends' else [t.InputPrivacyValueAllowUsers([u.get_input_user(await self._client.get_input_entity(value)) for value in opts['selected_user_ids']])]
            media=await self._media(p['media'][0],peer)
            areas=[t.MediaAreaUrl(coordinates=t.MediaAreaCoordinates(x=l['x'],y=l['y'],w=l['width'],h=l['height'],rotation=l['rotation']),url=l['url']) for l in opts['links']]
            result=await self._client(f.stories.SendStoryRequest(peer=peer,media=media,privacy_rules=privacy,caption=p['media'][0]['caption'],random_id=p['random_id'],period=opts['period_seconds'],pinned=opts['pinned'],noforwards=opts['noforwards'],media_areas=areas or None))
        elif action=='delete_story':
            result=await self._client(f.stories.DeleteStoriesRequest(peer=peer,id=p['story_ids']))
            return {'deleted':bool(result),'story_ids':list(result),'not_deleted_ids':[x for x in p['story_ids'] if x not in result],'chat':chat}
        elif action=='edit_story':
            media=await self._media(p['media'][0],peer) if p['media'] else None
            result=await self._client(f.stories.EditStoryRequest(peer=peer,id=p['story_id'],caption=p['caption'],media=media))
            return {'edited':True,'story_ids':[p['story_id']],'chat':chat}
        else:
            raise BackendError('unsupported_operation','This action is not implemented.')
        message_ids=returned_ids(result)
        if not message_ids:
            raise BackendError('result_unconfirmed','Telegram accepted an operation but no resulting ID was returned. Check Telegram; do not retry.')
        if action=='publish_story':
            return {'published':True,'story_ids':message_ids,'chat':chat,
                    'story_urls':[f"https://t.me/{chat['username']}/s/{identifier}" if chat.get('username') else None for identifier in message_ids]}
        return {'sent':True,'chat':chat,'message_ids':message_ids,'message_urls':[self._link(chat,id) for id in message_ids],
                'scheduled':bool(options.get('schedule_at')),'publication_type':'story' if action=='publish_story' else 'native_article' if action=='publish_article' else 'message'}
