"""TZ-bot-interaction 4.4-4.8 on a fabricated Telethon client: bot rights, channel posts, inline mode, BotFather."""
import asyncio
import base64
import hashlib
import json
from datetime import datetime,timezone
from types import SimpleNamespace as NS

import pytest
from starlette.testclient import TestClient
from telethon import errors
from telethon.tl import functions as f,types as t

import bot_interaction
import botfather
from bot_interaction import BotPolicy
from extended_backend import ExtendedBackend
from telegram_backend import BackendError
from test_bot_interaction import BotClient,NOW,callbacks,cb,inline,run
from test_server import bearer,rpc
from test_oauth_owner import oauth
from server import create_app

CHANNEL=-1001000000001
GROUP=-1001000000002
FATHER=93372553
TOKEN='123456789:ABCdefGHIjklMNOpqrsTUVwxyz0123456789abc'


def chan(identifier,title,**flags):
    shim=type('Channel',(),{})()
    shim.id,shim.title,shim.username,shim.access_hash=identifier,title,None,5
    shim.broadcast=flags.get('broadcast',False)
    shim.megagroup=flags.get('megagroup',False)
    shim.forum=flags.get('forum',False)
    return shim


class RpcFailure(Exception):
    def __init__(self,message):
        self.message=message


class ChannelClient(BotClient):
    def __init__(self):
        super().__init__()
        self.channel=chan(CHANNEL,'Тест_контент_завод',broadcast=True)
        self.group=chan(GROUP,'Рабочая группа',megagroup=True)
        self.human.username='adresat'
        self.father=t.User(id=FATHER,access_hash=9,first_name='BotFather',username='BotFather',bot=True)
        self.messages.update({CHANNEL:{},GROUP:{},FATHER:{}})
        self.participants={}
        self.admins=[]
        self.kicked,self.pinned,self.deleted=[],[],[]
        self.send_failure=None
        self.inline_results=[t.BotInlineResult(id='r1',type='article',send_message=t.BotInlineMessageText(message='Курс 90,5'),title='Курс',description='USD')]

    def add(self,chat,identifier,text='Пост',markup=None,out=False,edit_date=None,**extra):
        message=super().add(chat,identifier,text,markup,out,edit_date)
        for key,value in extra.items():
            setattr(message,key,value)
        return message

    async def iter_dialogs(self):
        async for dialog in super().iter_dialogs():
            yield dialog
        yield NS(id=CHANNEL,entity=self.channel,name=self.channel.title,is_channel=True,is_group=False)
        yield NS(id=GROUP,entity=self.group,name=self.group.title,is_channel=True,is_group=False)
        yield NS(id=FATHER,entity=self.father,name='BotFather',is_channel=False,is_group=False)

    async def get_input_entity(self,entity):
        if isinstance(entity,str):
            entity=await self.get_entity(entity)
        if type(entity).__name__=='Channel':
            return t.InputPeerChannel(abs(entity.id)-1000000000000,5)
        return await super().get_input_entity(entity)

    async def get_entity(self,name):
        known={'helper_bot':self.bot,'other_bot':self.other_bot,'adresat':self.human,'BotFather':self.father}
        if name.lstrip('@') not in known:
            raise errors.UsernameNotOccupiedError(request=None)
        return known[name.lstrip('@')]

    async def iter_participants(self,entity,filter=None,limit=None):
        for user,participant in self.admins:
            yield NS(id=user.id,first_name=user.first_name,last_name='',username=user.username,bot=getattr(user,'bot',False),deleted=False,participant=participant)

    async def kick_participant(self,entity,user):
        self.kicked.append(user)

    async def pin_message(self,entity,identifier,notify=False):
        self.pinned.append(('pin',identifier))

    async def unpin_message(self,entity,identifier=None):
        self.pinned.append(('unpin',identifier))

    async def delete_messages(self,entity,identifiers,revoke=True):
        self.deleted.append((list(identifiers),revoke))

    def father_reply(self,request):
        text=request.message if hasattr(request,'message') else ''
        script={'/setdescription':'Choose a bot to change description.','/setabouttext':'Choose a bot to change the about section.','/setname':'Choose a bot to change the name.',
                '/setcommands':'Choose a bot to change the list of commands.','/setuserpic':'Choose a bot to change profile picture.','/cancel':'Operation cancelled.'}
        if text in script:
            reply=script[text]
        elif text.startswith('@'):
            reply='Invalid bot selected.' if text=='@bad_bot' else 'OK. Send me the new value.'
        else:
            reply='Success! Updated. Old token '+TOKEN+' stays secret.' if text else 'Success! Profile photo updated.'
        top=max(self.messages[FATHER],default=1000)
        self.add(FATHER,top+1,text or '[image]',out=True)
        self.add(FATHER,top+2,reply)

    async def __call__(self,request):
        if isinstance(request,f.channels.GetParticipantRequest):
            self.requests.append(request)
            bytes(request)
            found=self.participants.get(request.participant.user_id)
            if found is None:
                raise errors.UserNotParticipantError(request=None)
            return NS(participant=found)
        if isinstance(request,f.messages.GetInlineBotResultsRequest):
            self.requests.append(request)
            bytes(request)
            return NS(query_id=4242,results=self.inline_results,next_offset='',cache_time=60)
        if self.send_failure and isinstance(request,(f.messages.SendMessageRequest,f.messages.SendMediaRequest,f.messages.EditMessageRequest,f.messages.SendInlineBotResultRequest)):
            self.requests.append(request)
            raise self.send_failure
        result=await super().__call__(request)
        if isinstance(request,f.channels.EditAdminRequest):
            uid=request.user_id.user_id
            flags=('post_messages','edit_messages','delete_messages','pin_messages','invite_users','change_info','ban_users','add_admins','manage_call','manage_topics')
            if any(getattr(request.admin_rights,flag,False) for flag in flags):
                self.participants[uid]=t.ChannelParticipantAdmin(user_id=uid,promoted_by=999,date=NOW,admin_rights=request.admin_rights,rank=request.rank or None)
            else:
                self.participants[uid]=t.ChannelParticipant(user_id=uid,date=NOW)
        peer=getattr(request,'peer',None)
        if getattr(peer,'user_id',None)==FATHER and isinstance(request,(f.messages.SendMessageRequest,f.messages.SendMediaRequest)):
            self.father_reply(request)
        return result


@pytest.fixture
def world(tmp_path):
    client=ChannelClient()
    engine=ExtendedBackend(client=client,allow_send=True,output_dir=tmp_path/'output',drafts_db=tmp_path/'drafts.sqlite',bot_policy=BotPolicy(None),allow_botfather=False)
    return engine,client


def stage(engine,data=b'fabricated image bytes',name='карточка.png',mime='image/png'):
    async def operation():
        pending=await engine.files.begin(name,mime,len(data),hashlib.sha256(data).hexdigest())
        return await engine.files.append(pending['upload_id'],0,base64.b64encode(data).decode(),True)
    return asyncio.run(operation())


def execute(engine,draft):
    return run(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))


def sent(client,kind):
    return [r for r in client.requests if isinstance(r,kind)]


def member(client,admin_rights=None,rank=None):
    if admin_rights is None:
        client.participants[500]=t.ChannelParticipant(user_id=500,date=NOW)
    else:
        client.participants[500]=t.ChannelParticipantAdmin(user_id=500,promoted_by=999,date=NOW,admin_rights=admin_rights,rank=rank)


# ---- 4.5 rights of bots ---------------------------------------------------------------------------------------

def test_add_bot_admin_shows_was_now_and_sends_exactly_the_reviewed_rights(world):
    engine,client=world
    draft=run(engine.prepare_add_bot_admin(CHANNEL,'@helper_bot',['post_messages','edit_messages']))
    preview=draft['preview']
    assert preview['changed_rights']==['post_messages','edit_messages'] and preview['rights']['post_messages']=={'was':False,'now':True,'changed':True}
    assert preview['rights']['add_admins']['now'] is False and preview['title']=={'was':None,'now':None} and not client.requests[1:]
    assert engine.required_scope(draft['draft_id'])=='telegram.manage'
    result=execute(engine,draft)
    request=sent(client,f.channels.EditAdminRequest)[0]
    on={right for right in ('post_messages','edit_messages','delete_messages','pin_messages','invite_users','change_info','ban_users','add_admins','manage_call','manage_topics') if getattr(request.admin_rights,right)}
    assert on=={'post_messages','edit_messages'} and request.rank=='' and result['changed'] and result['rights_now']==['post_messages','edit_messages']
    assert execute(engine,draft)['already_executed'] and len(sent(client,f.channels.EditAdminRequest))==1


def test_add_admins_is_never_granted_and_rights_must_fit_the_chat(world):
    engine,client=world
    with pytest.raises(BackendError) as error:
        run(engine.prepare_add_bot_admin(CHANNEL,'@helper_bot',['post_messages','add_admins']))
    assert error.value.code=='forbidden_right'
    with pytest.raises(BackendError) as error:
        run(engine.prepare_add_bot_admin(CHANNEL,'@helper_bot',['ban_users']))
    assert error.value.code=='invalid_input' and 'ban_users' in str(error.value)
    with pytest.raises(BackendError) as error:
        run(engine.prepare_add_bot_admin(GROUP,'@helper_bot',['post_messages']))
    assert error.value.code=='invalid_input'
    with pytest.raises(BackendError) as error:
        run(engine.prepare_add_bot_admin(GROUP,'@helper_bot',['manage_topics']))
    assert error.value.code=='invalid_input'
    assert run(engine.prepare_add_bot_admin(GROUP,'@helper_bot',['ban_users','delete_messages']))['preview']['changed_rights']==['delete_messages','ban_users']
    with pytest.raises(BackendError) as error:
        run(engine.prepare_add_bot_admin(CHANNEL,'@helper_bot',[]))
    assert error.value.code=='invalid_input'
    with pytest.raises(BackendError) as error:
        run(engine.prepare_add_bot_admin(100,'@helper_bot',['post_messages']))
    assert error.value.code=='unsupported_operation'
    assert not sent(client,f.channels.EditAdminRequest)


def test_people_are_out_of_scope_and_unknown_bots_are_reported(world):
    engine,_=world
    for call in (lambda:run(engine.prepare_add_bot_admin(CHANNEL,'@adresat',['post_messages'])),lambda:run(engine.prepare_demote_bot(CHANNEL,100)),lambda:run(engine.prepare_remove_bot(CHANNEL,'@adresat'))):
        with pytest.raises(BackendError) as error:
            call()
        assert error.value.code=='out_of_scope'
    with pytest.raises(BackendError) as error:
        run(engine.prepare_add_bot_admin(CHANNEL,'@nobody_here',['post_messages']))
    assert error.value.code=='user_not_accessible'


def test_edit_rights_removes_one_right_and_keeps_the_rest(world):
    engine,client=world
    member(client,t.ChatAdminRights(post_messages=True,edit_messages=True,delete_messages=True),'Бот')
    with pytest.raises(BackendError) as error:
        run(engine.prepare_add_bot_admin(CHANNEL,'@helper_bot',['pin_messages']))
    assert error.value.code=='already_admin' and set(error.value.diagnostics['current_rights'])=={'post_messages','edit_messages','delete_messages'}
    draft=run(engine.prepare_edit_bot_rights(CHANNEL,'@helper_bot',revoke=['edit_messages']))
    assert draft['preview']['changed_rights']==['edit_messages'] and draft['preview']['destructive'] is True
    assert draft['preview']['rights']['edit_messages']=={'was':True,'now':False,'changed':True} and draft['preview']['title']=={'was':'Бот','now':'Бот'}
    execute(engine,draft)
    request=sent(client,f.channels.EditAdminRequest)[-1]
    assert request.admin_rights.post_messages and request.admin_rights.delete_messages and not request.admin_rights.edit_messages and request.rank=='Бот'
    with pytest.raises(BackendError) as error:
        run(engine.prepare_edit_bot_rights(CHANNEL,'@helper_bot',grant=['post_messages']))
    assert error.value.code=='no_change'
    with pytest.raises(BackendError) as error:
        run(engine.prepare_edit_bot_rights(CHANNEL,'@helper_bot',grant=['pin_messages'],revoke=['pin_messages']))
    assert error.value.code=='invalid_input'
    with pytest.raises(BackendError) as error:
        run(engine.prepare_edit_bot_rights(CHANNEL,'@helper_bot',grant=['add_admins']))
    assert error.value.code=='forbidden_right'


def test_existing_add_admins_is_preserved_not_silently_revoked_by_an_edit(world):
    engine,client=world
    member(client,t.ChatAdminRights(post_messages=True,add_admins=True))
    draft=run(engine.prepare_edit_bot_rights(CHANNEL,'@helper_bot',grant=['pin_messages']))
    assert draft['preview']['rights']['add_admins']=={'was':True,'now':True,'changed':False}
    execute(engine,draft)
    assert sent(client,f.channels.EditAdminRequest)[-1].admin_rights.add_admins


def test_demote_remove_and_title_in_a_supergroup(world):
    engine,client=world
    member(client,t.ChatAdminRights(delete_messages=True,ban_users=True),'Старый')
    title=run(engine.prepare_set_bot_title(GROUP,'@helper_bot','Новый'))
    assert title['preview']['title']=={'was':'Старый','now':'Новый'} and title['preview']['changed_rights']==[]
    execute(engine,title)
    last=sent(client,f.channels.EditAdminRequest)[-1]
    assert last.rank=='Новый' and last.admin_rights.delete_messages and last.admin_rights.ban_users
    with pytest.raises(BackendError) as error:
        run(engine.prepare_set_bot_title(GROUP,'@helper_bot','Новый'))
    assert error.value.code=='no_change'
    demote=run(engine.prepare_demote_bot(GROUP,'@helper_bot'))
    assert demote['preview']['changed_rights']==['delete_messages','ban_users'] and demote['preview']['destructive']
    execute(engine,demote)
    assert not any(getattr(sent(client,f.channels.EditAdminRequest)[-1].admin_rights,right) for right in ('delete_messages','ban_users','add_admins'))
    with pytest.raises(BackendError) as error:
        run(engine.prepare_set_bot_title(GROUP,'@helper_bot','Ещё'))
    assert error.value.code=='not_admin'
    member(client)
    removal=run(engine.prepare_remove_bot(GROUP,'@helper_bot'))
    assert removal['preview']['destructive'] and removal['preview']['effect']=='The bot is removed from the chat.'
    assert execute(engine,removal)['operation']=='remove' and client.kicked
    client.participants.clear()
    with pytest.raises(BackendError) as error:
        run(engine.prepare_remove_bot(GROUP,'@helper_bot'))
    assert error.value.code=='not_a_member'
    with pytest.raises(BackendError) as error:
        run(engine.prepare_demote_bot(GROUP,'@helper_bot'))
    assert error.value.code=='not_admin'


def test_channels_have_no_custom_titles_and_inherited_rights_do_not_block_an_edit(world):
    engine,client=world
    for call in (lambda:run(engine.prepare_add_bot_admin(CHANNEL,'@helper_bot',['post_messages'],'Бот')),lambda:run(engine.prepare_set_bot_title(CHANNEL,'@helper_bot','Бот'))):
        with pytest.raises(BackendError) as error:
            call()
        assert error.value.code=='invalid_input' and 'supergroups' in str(error.value)
    member(client,t.ChatAdminRights(post_messages=True,ban_users=True))
    with pytest.raises(BackendError) as error:
        run(engine.prepare_edit_bot_rights(CHANNEL,'@helper_bot',revoke=['post_messages'],title='Бот'))
    assert error.value.code=='invalid_input'
    draft=run(engine.prepare_edit_bot_rights(CHANNEL,'@helper_bot',revoke=['post_messages']))
    execute(engine,draft)
    request=sent(client,f.channels.EditAdminRequest)[-1]
    assert request.admin_rights.ban_users and not request.admin_rights.post_messages


def test_list_admins_flags_bots(world):
    engine,client=world
    client.admins=[(client.owner,t.ChannelParticipantCreator(user_id=999,admin_rights=t.ChatAdminRights(post_messages=True),rank='Владелец')),
                   (client.bot,t.ChannelParticipantAdmin(user_id=500,promoted_by=999,date=NOW,admin_rights=t.ChatAdminRights(post_messages=True)))]
    result=run(engine.list_admins(CHANNEL))
    assert result['bots']==[500] and result['admins'][0]['is_creator'] and result['admins'][1]['rights']['post_messages'] and not result['admins'][1]['rights']['add_admins']
    with pytest.raises(BackendError) as error:
        run(engine.list_admins(100))
    assert error.value.code=='unsupported_operation'


# ---- 4.6 channel posts ----------------------------------------------------------------------------------------

def test_publish_post_with_buttons_schedule_and_clear_refusal(world):
    engine,client=world
    draft=run(engine.prepare_publish_post(CHANNEL,'Новый товар',url_buttons=[{'text':'Купить','url':'https://example.com/buy'}],silent=True))
    assert draft['preview']['publication_type']=='channel_post' and draft['preview']['recipient']['type']=='channel' and draft['preview']['buttons'][0]['text']=='Купить'
    assert engine.required_scope(draft['draft_id'])=='telegram.send'
    execute(engine,draft)
    request=sent(client,f.messages.SendMessageRequest)[-1]
    assert request.message=='Новый товар' and request.silent and isinstance(request.reply_markup,t.ReplyInlineMarkup)
    assert request.reply_markup.rows[0].buttons[0].type.url=='https://example.com/buy'
    for bad in ('javascript:alert(1)','https://user@example.com/a','ftp://example.com','https://exa mple.com'):
        with pytest.raises(BackendError) as error:
            run(engine.prepare_publish_post(CHANNEL,'x',url_buttons=[{'text':'x','url':bad}]))
        assert error.value.code=='invalid_input'
    refused=run(engine.prepare_publish_post(CHANNEL,'С кнопкой',url_buttons=[{'text':'Купить','url':'https://example.com/buy'}]))
    client.send_failure=RpcFailure('REPLY_MARKUP_INVALID')
    with pytest.raises(BackendError) as error:
        execute(engine,refused)
    assert error.value.code=='buttons_not_supported'
    client.send_failure=None
    assert execute(engine,refused)['sent'] is True
    scheduled=run(engine.prepare_publish_post(CHANNEL,'Позже',schedule_at='2099-01-01T12:00:00+03:00'))
    assert scheduled['preview']['options']['schedule_at']=='2099-01-01T12:00:00+03:00'
    with pytest.raises(BackendError):
        run(engine.prepare_publish_post(CHANNEL,'Позже',schedule_at='2099-01-01T12:00:00'))
    with pytest.raises(BackendError) as error:
        run(engine.prepare_publish_post(100,'Не канал'))
    assert error.value.code=='invalid_input'


def test_publish_post_with_a_photo_uses_the_staged_original(world):
    engine,client=world
    asset=stage(engine)
    draft=run(engine.prepare_publish_post(CHANNEL,'Подпись',file_id=asset['file_id']))
    assert draft['preview']['media'][0]['sha256']==asset['sha256'] and draft['preview']['media'][0]['kind']=='photo'
    execute(engine,draft)
    request=sent(client,f.messages.SendMediaRequest)[-1]
    assert isinstance(request.media,t.InputMediaUploadedPhoto) and request.message=='Подпись'
    text_file=stage(engine,b'plain text','заметка.txt','text/plain')
    with pytest.raises(BackendError) as error:
        run(engine.prepare_publish_post(CHANNEL,'x',file_id=text_file['file_id']))
    assert error.value.code=='unsupported_media'


def test_edit_post_text_shows_a_word_diff_and_needs_manage(world):
    engine,client=world
    client.add(CHANNEL,10,text='Холодильник Beko, цена 31 900 ₽, артикул A-1',chat_id=CHANNEL)
    draft=run(engine.prepare_edit_post_text(CHANNEL,10,'Холодильник Beko, цена 29 900 ₽, артикул A-1'))
    preview=draft['preview']
    assert preview['was'].startswith('Холодильник') and preview['now'].endswith('A-1')
    assert [s['text'] for s in preview['diff'] if s['op']=='delete']==['31'] and [s['text'] for s in preview['diff'] if s['op']=='insert']==['29']
    assert engine.required_scope(draft['draft_id'])=='telegram.manage'
    result=execute(engine,draft)
    request=sent(client,f.messages.EditMessageRequest)[-1]
    assert result['edited'] and request.id==10 and request.message=='Холодильник Beko, цена 29 900 ₽, артикул A-1'
    with pytest.raises(BackendError) as error:
        run(engine.prepare_edit_post_text(CHANNEL,10,client.messages[CHANNEL][10].message))
    assert error.value.code=='no_change'


def test_messages_written_by_others_in_private_chats_are_not_editable_by_the_user(world):
    engine,client=world
    client.add(500,20,text='Превью товара',markup=inline([cb('✅')]))
    with pytest.raises(BackendError) as error:
        run(engine.prepare_edit_post_text(500,20,'Другой текст'))
    assert error.value.code=='not_editable_by_user' and 'bot' in str(error.value)
    with pytest.raises(BackendError) as error:
        run(engine.prepare_message_action('edit',500,[20],text='Другой текст'))
    assert error.value.code=='not_editable_by_user'
    client.add(GROUP,21,text='Чужое в группе',chat_id=GROUP)
    with pytest.raises(BackendError) as error:
        run(engine.prepare_edit_post_text(GROUP,21,'Правка'))
    assert error.value.code=='not_editable_by_user'
    client.add(500,22,text='Моё сообщение боту',out=True)
    assert run(engine.prepare_message_action('edit',500,[22],text='Исправлено'))['preview']['action']=='edit'
    client.add(CHANNEL,23,text='Пост бота в канале',chat_id=CHANNEL)
    client.send_failure=RpcFailure('X')
    client.send_failure=type('MessageAuthorRequiredError',(Exception,),{})()
    draft=run(engine.prepare_edit_post_text(CHANNEL,23,'Правка поста'))
    with pytest.raises(BackendError) as error:
        execute(engine,draft)
    assert error.value.code=='not_editable_by_user'
    client.send_failure=None
    assert execute(engine,draft)['edited']


def test_replace_post_media_keeps_or_changes_the_caption(world):
    engine,client=world
    client.add(CHANNEL,10,text='Подпись остаётся',chat_id=CHANNEL)
    asset=stage(engine,b'new photo bytes','новое.jpg','image/jpeg')
    draft=run(engine.prepare_replace_post_media(CHANNEL,10,asset['file_id']))
    assert draft['preview']['caption']['kept'] is True and draft['preview']['media'][0]['sha256']==asset['sha256']
    execute(engine,draft)
    request=sent(client,f.messages.EditMessageRequest)[-1]
    assert isinstance(request.media,t.InputMediaUploadedPhoto) and request.message=='Подпись остаётся'
    changed=run(engine.prepare_replace_post_media(CHANNEL,10,asset['file_id'],caption='Новая подпись'))
    assert [s['op'] for s in changed['preview']['caption']['diff']].count('insert')>=1
    execute(engine,changed)
    assert sent(client,f.messages.EditMessageRequest)[-1].message=='Новая подпись'
    assert engine.required_scope(changed['draft_id'])=='telegram.manage'


def test_delete_post_shows_the_full_text_and_deletes_for_everyone(world):
    engine,client=world
    long_text='Очень длинный пост. '*60
    client.add(CHANNEL,10,text=long_text,chat_id=CHANNEL,views=120)
    draft=run(engine.prepare_delete_post(CHANNEL,[10]))
    assert draft['preview']['messages'][0]['text']==long_text and draft['preview']['messages'][0]['views']==120
    assert draft['preview']['destructive'] and draft['preview']['revoke_for_everyone'] and engine.required_scope(draft['draft_id'])=='telegram.manage'
    assert not client.deleted
    assert execute(engine,draft)['deleted'] and client.deleted==[([10],True)]
    with pytest.raises(BackendError) as error:
        run(engine.prepare_delete_post(500,[1]))
    assert error.value.code=='invalid_input'


def test_pin_unpin_and_post_stats(world):
    engine,client=world
    reactions=NS(results=[NS(reaction=NS(emoticon='👍'),count=7),NS(reaction=NS(document_id=55),count=2)])
    client.add(CHANNEL,10,text='Пост',chat_id=CHANNEL,views=1000,forwards=12,reactions=reactions,replies=NS(replies=4),pinned=False)
    pin=run(engine.prepare_pin_post(CHANNEL,10))
    assert pin['preview']['operation']=='pin' and engine.required_scope(pin['draft_id'])=='telegram.manage'
    execute(engine,pin)
    execute(engine,run(engine.prepare_pin_post(CHANNEL,10,unpin=True)))
    assert client.pinned==[('pin',10),('unpin',10)]
    stats=run(engine.get_post_stats(CHANNEL,10))
    assert stats['views']==1000 and stats['forwards']==12 and stats['comments']==4 and stats['reactions']==[{'reaction':'👍','count':7},{'reaction':55,'count':2}]
    client.add(CHANNEL,11,text='Без счётчиков',chat_id=CHANNEL)
    assert run(engine.get_post_stats(CHANNEL,11))['views'] is None


# ---- 4.4 inline mode ------------------------------------------------------------------------------------------

def test_inline_query_then_one_reviewed_send(world):
    engine,client=world
    found=run(engine.inline_query(100,'@helper_bot','курс'))
    assert found['results'][0]['title']=='Курс' and found['results'][0]['text_preview']=='Курс 90,5' and found['next_offset'] is None and 'untrusted' in found['data_trust']
    assert 'query_id' not in json.dumps(found) and sent(client,f.messages.GetInlineBotResultsRequest)[0].query=='курс'
    ref=found['results'][0]['result_ref']
    draft=run(engine.prepare_send_inline_result(100,ref))
    assert draft['preview']['bot']['username']=='helper_bot' and draft['preview']['result']['text']=='Курс 90,5' and not sent(client,f.messages.SendInlineBotResultRequest)
    assert engine.required_scope(draft['draft_id'])=='telegram.send'
    execute(engine,draft)
    request=sent(client,f.messages.SendInlineBotResultRequest)[0]
    assert request.query_id==4242 and request.id=='r1'
    assert execute(engine,draft)['already_executed'] and len(sent(client,f.messages.SendInlineBotResultRequest))==1


def test_inline_errors_are_clean(world):
    engine,client=world
    with pytest.raises(BackendError) as error:
        run(engine.prepare_send_inline_result(100,'r_unknown'))
    assert error.value.code=='inline_result_unknown'
    with pytest.raises(BackendError) as error:
        run(engine.inline_query(100,'@other_bot','x'))
    assert error.value.code=='inline_not_supported'
    with pytest.raises(BackendError) as error:
        run(engine.inline_query(100,'@adresat','x'))
    assert error.value.code=='not_a_bot'
    ref=run(engine.inline_query(100,'@helper_bot','x'))['results'][0]['result_ref']
    with pytest.raises(BackendError) as error:
        run(engine.prepare_send_inline_result(500,ref))
    assert error.value.code=='invalid_input'
    draft=run(engine.prepare_send_inline_result(100,ref))
    client.send_failure=type('QueryIdInvalidError',(Exception,),{})()
    with pytest.raises(BackendError) as error:
        execute(engine,draft)
    assert error.value.code=='inline_result_expired'
    client.send_failure=None
    assert execute(engine,draft)['sent']
    engine._clock=lambda:NOW.timestamp()+10000
    with pytest.raises(BackendError) as error:
        run(engine.prepare_send_inline_result(100,ref))
    assert error.value.code=='inline_result_unknown'


# ---- 4.8 BotFather ----------------------------------------------------------------------------------------------

@pytest.fixture
def father(world,monkeypatch):
    engine,client=world
    engine.allow_botfather=True
    monkeypatch.setattr(botfather,'SETTLE_SECONDS',0.01)
    monkeypatch.setattr(botfather,'POLL_SECONDS',0.01)
    monkeypatch.setattr(botfather,'REPLY_WAIT',0.3)
    return engine,client


def father_messages(client):
    return [m.message for m in client.messages[FATHER].values() if m.out]


def test_botfather_is_disabled_by_default(world):
    engine,client=world
    with pytest.raises(BackendError) as error:
        run(engine.prepare_botfather_change('@helper_bot','set_description','Описание'))
    assert error.value.code=='botfather_disabled' and not client.requests


def test_botfather_chain_is_previewed_executed_in_order_and_tokens_are_redacted(father):
    engine,client=father
    draft=run(engine.prepare_botfather_change('helper_bot','set_description','Новое описание'))
    assert [step['send'] for step in draft['preview']['messages_in_order']]==['/setdescription','@helper_bot','Новое описание'] and not client.requests
    assert engine.required_scope(draft['draft_id'])=='telegram.manage'
    result=execute(engine,draft)
    assert father_messages(client)==['/setdescription','@helper_bot','Новое описание'] and result['confirmed_by_botfather'] and result['changed']
    assert TOKEN not in json.dumps(result,ensure_ascii=False) and '[token redacted]' in result['steps'][-1]['reply']
    history=run(engine.read_history(FATHER,limit=10))
    assert TOKEN not in json.dumps(history,ensure_ascii=False) and any('[token redacted]' in m['text'] for m in history['messages'])
    shown=run(engine.get_keyboard(FATHER,max(client.messages[FATHER])))
    assert TOKEN not in json.dumps(shown,ensure_ascii=False)
    assert TOKEN not in json.dumps(run(engine.search_history(FATHER,'token')),ensure_ascii=False)


def test_botfather_commands_name_about_and_picture(father):
    engine,client=father
    draft=run(engine.prepare_botfather_change('@helper_bot','set_commands',commands=[{'command':'/start','description':'Запуск'},{'command':'status','description':'Статус завода'}]))
    assert draft['preview']['messages_in_order'][2]['send']=='start - Запуск\nstatus - Статус завода'
    execute(engine,draft)
    assert father_messages(client)[-3:]==['/setcommands','@helper_bot','start - Запуск\nstatus - Статус завода']
    execute(engine,run(engine.prepare_botfather_change('@helper_bot','set_name','Помощник')))
    execute(engine,run(engine.prepare_botfather_change('@helper_bot','set_about','Коротко о боте')))
    assert {'/setname','/setabouttext'}<=set(father_messages(client))
    asset=stage(engine,b'png bytes','аватар.png','image/png')
    picture=run(engine.prepare_botfather_change('@helper_bot','set_userpic',file_id=asset['file_id']))
    assert picture['preview']['messages_in_order'][2]['sha256']==asset['sha256']
    assert execute(engine,picture)['changed'] and sent(client,f.messages.SendMediaRequest)
    pdf=stage(engine,b'%PDF','x.pdf','application/pdf')
    with pytest.raises(BackendError) as error:
        run(engine.prepare_botfather_change('@helper_bot','set_userpic',file_id=pdf['file_id']))
    assert error.value.code=='unsupported_media'
    for bad in ({'command':'BAD NAME','description':'описание'},{'command':'ok','description':'x'}):
        with pytest.raises(BackendError):
            run(engine.prepare_botfather_change('@helper_bot','set_commands',commands=[bad]))


def test_botfather_dangerous_commands_are_impossible_even_with_the_flag(father):
    engine,client=father
    for text in ('/token','/revoke','  /deletebot','/transfer @x'):
        with pytest.raises(BackendError) as error:
            run(engine.prepare_botfather_change('@helper_bot','set_description',text))
        assert error.value.code=='forbidden_botfather_command'
    for call in (lambda:run(engine.prepare_send(FATHER,'/token')),lambda:run(engine.prepare_send(FATHER,'/revoke')),lambda:run(engine.prepare_send(FATHER,'привет')),
                 lambda:run(engine.prepare_message_action('reply',FATHER,[1],text='/deletebot')),lambda:run(engine.prepare_message_action('forward',100,[4],target_chat_id=FATHER))):
        client.add(FATHER,1,text='x')
        client.add(100,4,text='y')
        with pytest.raises(BackendError) as error:
            call()
        assert error.value.code=='botfather_scenario_required'
    with pytest.raises(BackendError):
        run(engine.prepare_botfather_change('@helper_bot','delete_bot','x'))
    with pytest.raises(BackendError):
        run(engine.prepare_botfather_change('not a username','set_name','x'))
    assert not client.requests


def test_botfather_rejection_cancels_the_dialog_and_reports_the_steps(father):
    engine,client=father
    draft=run(engine.prepare_botfather_change('@bad_bot','set_name','Имя'))
    with pytest.raises(BackendError) as error:
        execute(engine,draft)
    assert error.value.code=='botfather_error' and error.value.diagnostics['steps'][-1]['reply']=='Invalid bot selected.'
    assert father_messages(client)[-1]=='/cancel' and 'Имя' not in father_messages(client)
    with pytest.raises(BackendError) as again:
        execute(engine,draft)
    assert again.value.code=='delivery_uncertain'


# ---- MCP surface ------------------------------------------------------------------------------------------------

@pytest.fixture
def web(world,oauth,tmp_path,monkeypatch):
    engine,client=world
    owner,_,_=oauth
    monkeypatch.setenv('TG_DATA_DIR',str(tmp_path))
    with TestClient(create_app(engine,owner),base_url=owner.public_url) as app:
        yield app,owner,engine,client


def tool(app,owner,scopes,name,arguments):
    return rpc(app,bearer(owner,scopes),'tools/call',{'name':name,'arguments':arguments}).json()['result']['structuredContent']


def test_manage_drafts_need_manage_scope_and_the_management_tool(web):
    app,owner,engine,client=web
    send=['telegram.read','telegram.send']
    manage=['telegram.read','telegram.send','telegram.manage']
    draft=tool(app,owner,send,'telegram_prepare_add_bot_admin',{'chat_id':CHANNEL,'bot':'@helper_bot','rights':['post_messages']})
    assert draft['preview']['operation']=='add'
    args={'draft_id':draft['draft_id'],'confirmation_hash':draft['confirmation_hash']}
    assert tool(app,owner,send,'telegram_execute_action',args)['error']=='insufficient_scope'
    assert tool(app,owner,send,'telegram_execute_management_action',args)['error']=='insufficient_scope' and not sent(client,f.channels.EditAdminRequest)
    assert tool(app,owner,manage,'telegram_execute_management_action',args)['changed'] is True and len(sent(client,f.channels.EditAdminRequest))==1
    plain=run(engine.prepare_send(100,'Обычное'))
    assert tool(app,owner,manage,'telegram_execute_management_action',{'draft_id':plain['draft_id'],'confirmation_hash':plain['confirmation_hash']})['error']=='invalid_action'


def test_schema_refuses_add_admins_and_unknown_rights(web):
    app,owner,_,client=web
    response=rpc(app,bearer(owner,['telegram.read']),'tools/call',{'name':'telegram_prepare_add_bot_admin','arguments':{'chat_id':CHANNEL,'bot':'@helper_bot','rights':['add_admins']}}).json()['result']
    assert response['isError'] is True and not client.requests


def test_post_tools_and_audit_through_mcp(web):
    app,owner,engine,client=web
    manage=['telegram.read','telegram.send','telegram.manage']
    client.add(CHANNEL,10,text='Старая цена 100',chat_id=CHANNEL)
    draft=tool(app,owner,manage,'telegram_prepare_edit_post_text',{'chat_id':CHANNEL,'message_id':10,'text':'Новая цена 90'})
    assert tool(app,owner,manage,'telegram_execute_management_action',{'draft_id':draft['draft_id'],'confirmation_hash':draft['confirmation_hash']})['edited']
    published=tool(app,owner,manage,'telegram_prepare_publish_post',{'chat_id':CHANNEL,'text':'Пост'})
    assert tool(app,owner,manage,'telegram_execute_action',{'draft_id':published['draft_id'],'confirmation_hash':published['confirmation_hash']})['sent']
    unpin=tool(app,owner,manage,'telegram_prepare_unpin_post',{'chat_id':CHANNEL,'message_id':10})
    assert unpin['preview']['operation']=='unpin'
    forward=tool(app,owner,manage,'telegram_prepare_forward_post',{'chat_id':CHANNEL,'message_ids':[10],'target_chat_id':GROUP,'drop_author':True})
    assert forward['preview']['drop_author'] is True
    entries=engine.audit.entries()
    assert {'edit_post','send_message'}<={e['action'] for e in entries}
    dumped=json.dumps(entries,ensure_ascii=False)
    assert 'Новая цена' not in dumped and 'Старая цена' not in dumped


def test_botfather_and_inline_tools_through_mcp(web):
    app,owner,engine,client=web
    manage=['telegram.read','telegram.send','telegram.manage']
    off=tool(app,owner,manage,'telegram_prepare_botfather_change',{'bot':'@helper_bot','operation':'set_name','text':'Имя'})
    assert off['error']=='botfather_disabled'
    found=tool(app,owner,manage,'telegram_inline_query',{'chat_id':100,'bot':'@helper_bot','query':'курс'})
    draft=tool(app,owner,manage,'telegram_prepare_send_inline_result',{'chat_id':100,'result_ref':found['results'][0]['result_ref']})
    assert draft['preview']['result']['title']=='Курс'


def test_every_new_tool_is_listed_with_safe_annotations(web):
    app,owner,_,_=web
    tools={x['name']:x for x in rpc(app,bearer(owner,['telegram.read']),'tools/list').json()['result']['tools']}
    assert len(tools)==108
    reads=['telegram_list_admins','telegram_get_post_stats','telegram_inline_query']
    prepares=['telegram_prepare_add_bot_admin','telegram_prepare_edit_bot_rights','telegram_prepare_demote_bot','telegram_prepare_remove_bot','telegram_prepare_set_bot_title',
              'telegram_prepare_publish_post','telegram_prepare_edit_post_text','telegram_prepare_replace_post_media','telegram_prepare_delete_post','telegram_prepare_pin_post',
              'telegram_prepare_unpin_post','telegram_prepare_forward_post','telegram_prepare_send_inline_result','telegram_prepare_botfather_change']
    assert all(name in tools for name in reads+prepares)
    for name in reads+prepares:
        assert tools[name]['annotations']['readOnlyHint'] is True
    assert 'add_admins' not in json.dumps(tools['telegram_prepare_add_bot_admin']['inputSchema'])
    assert 'untrusted' in tools['telegram_inline_query']['description']
