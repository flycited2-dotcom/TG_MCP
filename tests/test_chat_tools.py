"""Chat list settings, polls, scheduled messages, chat administration, invite links, topics, blocking: all reviewed first."""
import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace as NS

import pytest
from starlette.testclient import TestClient
from telethon import errors,utils
from telethon.tl import functions as f,types as t

import jobs
from bot_interaction import BotPolicy
from extended_backend import ExtendedBackend
from jobs import Limits
from telegram_backend import BackendError
from test_groups_folders import GroupClient,NOW,channel,cid,dialog,folder,go,sent_requests
from test_server import bearer,rpc
from test_oauth_owner import oauth
from server import create_app


class ChatClient(GroupClient):
    def __init__(self):
        super().__init__()
        self.bot=t.User(id=500,access_hash=9,first_name='Помощник',username='helper_bot',bot=True)
        self.admin_group=channel(42,'Моя группа',megagroup=True,creator=True,participants_count=70,default_banned_rights=t.ChatBannedRights(until_date=None,change_info=True,pin_messages=True))
        self.forum=channel(41,'Форум',megagroup=True,forum=True,creator=True,participants_count=60)
        self.helper=channel(43,'Помощник группы',megagroup=True,participants_count=25,admin_rights=t.ChatAdminRights(invite_users=True))
        self.dialogs+=[dialog(self.bot),dialog(self.admin_group),dialog(self.forum),dialog(self.helper)]
        self.public['taken_name']=self.news
        self.taken={'taken_name'}
        self.fail={}
        self.history_offsets=[]

    async def __call__(self,request):
        for kind,error in list(self.fail.items()):
            if isinstance(request,kind):
                self.requests.append(request)
                raise error
        if isinstance(request,f.messages.SearchGlobalRequest):
            self.requests.append(request)
            bytes(request)
            return NS(messages=[NS(id=77,peer_id=t.PeerChannel(channel_id=self.barakholka.id),date=NOW,message='Куплю диван срочно',out=False)],chats=[self.barakholka],users=[])
        if isinstance(request,f.messages.GetScheduledHistoryRequest):
            self.requests.append(request)
            return NS(messages=[NS(id=5,date=NOW,message='Акция в пятницу',media=None),NS(id=6,date=NOW,message='Ещё акция',media=object())])
        if isinstance(request,f.messages.GetExportedChatInvitesRequest):
            self.requests.append(request)
            bytes(request)
            return NS(invites=[NS(link='https://t.me/+AAAAA11111',title='Реклама',usage=3,usage_limit=None,expire_date=None,request_needed=False,revoked=False,permanent=True)])
        if isinstance(request,f.messages.ExportChatInviteRequest):
            self.requests.append(request)
            bytes(request)
            return NS(link='https://t.me/+NEWLINK12345')
        if isinstance(request,f.messages.GetChatInviteImportersRequest):
            self.requests.append(request)
            bytes(request)
            return NS(importers=[NS(user_id=900,date=NOW,about='Хочу в чат'),NS(user_id=901,date=NOW,about='')],
                      users=[t.User(id=900,access_hash=5,first_name='Новичок',username='newbie'),t.User(id=901,access_hash=6,first_name='Второй')])
        if isinstance(request,f.messages.GetForumTopicsByIDRequest):
            self.requests.append(request)
            return NS(topics=[NS(title='Объявления')])
        if isinstance(request,f.channels.GetAdminLogRequest):
            self.requests.append(request)
            bytes(request)
            return NS(events=[NS(id=1,date=NOW,user_id=999,action=t.ChannelAdminLogEventActionToggleSlowMode(prev_value=0,new_value=30))],users=[self.owner])
        if isinstance(request,f.channels.CheckUsernameRequest):
            self.requests.append(request)
            bytes(request)
            return request.username not in self.taken
        if isinstance(request,f.messages.DeleteHistoryRequest):
            self.requests.append(request)
            bytes(request)
            return NS(offset=self.history_offsets.pop(0) if self.history_offsets else 0)
        return await super().__call__(request)

    async def get_entity(self,name):
        key=name.lstrip('@')
        if key=='helper_bot':
            return self.bot
        return await super().get_entity(name)


@pytest.fixture(autouse=True)
def instant(monkeypatch):
    async def quick(self,seconds):
        await asyncio.sleep(0)
    monkeypatch.setattr(jobs.Jobs,'_sleep',quick)


@pytest.fixture
def world(tmp_path):
    client=ChatClient()
    engine=ExtendedBackend(client=client,allow_send=True,output_dir=tmp_path/'output',drafts_db=tmp_path/'drafts.sqlite',bot_policy=BotPolicy(None),allow_botfather=False,
                           limits=Limits({'TG_CAMPAIGN_MIN_DELAY':'0'}))
    return engine,client


def execute(engine,draft):
    return go(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))


def stage(engine,data,name,mime):
    async def operation():
        pending=await engine.files.begin(name,mime,len(data),hashlib.sha256(data).hexdigest())
        return await engine.files.append(pending['upload_id'],0,base64.b64encode(data).decode(),True)
    return go(operation())


def failing(code,**kwargs):
    with pytest.raises(BackendError) as error:
        pass
    return error


# ---- chat list settings -----------------------------------------------------------------------------------

def test_mute_and_unmute_with_a_preview_and_the_exact_notification_settings(world):
    engine,client=world
    ids=[cid(client.barakholka),cid(client.news)]
    draft=go(engine.prepare_chat_setting(ids,'mute',hours=3))
    assert draft['preview']['will_apply']==2 and draft['preview']['reversible'] and engine.required_scope(draft['draft_id'])=='telegram.manage' and not client.requests
    result=execute(engine,draft)
    requests=sent_requests(client,f.account.UpdateNotifySettingsRequest)
    assert result['done']==2 and len(requests)==2 and isinstance(requests[0].peer,t.InputNotifyPeer)
    assert abs(requests[0].settings.mute_until-(engine._clock()+3*3600))<5
    forever=execute(engine,go(engine.prepare_chat_setting(ids[:1],'mute')))
    assert forever['done']==1 and sent_requests(client,f.account.UpdateNotifySettingsRequest)[-1].settings.mute_until==2147483647
    execute(engine,go(engine.prepare_chat_setting(ids[:1],'unmute')))
    assert sent_requests(client,f.account.UpdateNotifySettingsRequest)[-1].settings.mute_until==0


def test_archive_pin_and_mark_read_use_the_right_requests(world):
    engine,client=world
    news,private,group=cid(client.news),cid(client.friend),cid(client.barakholka)
    execute(engine,go(engine.prepare_chat_setting([news],'archive')))
    peer=sent_requests(client,f.folders.EditPeerFoldersRequest)[-1].folder_peers[0]
    assert peer.folder_id==1 and isinstance(peer.peer,t.InputPeerChannel)
    execute(engine,go(engine.prepare_chat_setting([cid(client.barakholka2)],'unarchive')))
    assert sent_requests(client,f.folders.EditPeerFoldersRequest)[-1].folder_peers[0].folder_id==0
    execute(engine,go(engine.prepare_chat_setting([group],'pin_chat')))
    pin=sent_requests(client,f.messages.ToggleDialogPinRequest)[-1]
    assert pin.pinned is True and isinstance(pin.peer,t.InputDialogPeer)
    result=execute(engine,go(engine.prepare_chat_setting([news,private],'mark_read')))
    assert result['done']==2 and sent_requests(client,f.channels.ReadHistoryRequest)[-1].channel.channel_id==client.news.id
    assert sent_requests(client,f.messages.ReadHistoryRequest)[-1].peer.user_id==client.friend.id


def test_chat_setting_skips_no_ops_and_reports_the_reason(world):
    engine,client=world
    draft=go(engine.prepare_chat_setting([cid(client.barakholka2),cid(client.news),-1009999999999],'archive'))
    rows={row['chat_id']:row for row in draft['preview']['chats']}
    assert rows[cid(client.barakholka2)]['reason']=='already_so' and rows[cid(client.news)]['status']=='will_apply' and rows[-1009999999999]['reason']=='not_in_your_chat_list'
    assert draft['preview']['will_apply']==1
    with pytest.raises(BackendError) as error:
        go(engine.prepare_chat_setting([cid(client.barakholka2)],'archive'))
    assert error.value.code=='nothing_to_change'
    for bad in (lambda:go(engine.prepare_chat_setting([cid(client.news)],'explode')),lambda:go(engine.prepare_chat_setting([cid(client.news)],'archive',hours=2)),
                lambda:go(engine.prepare_chat_setting([cid(client.news)],'mute',hours=0)),lambda:go(engine.prepare_chat_setting([],'mute')),
                lambda:go(engine.prepare_chat_setting([1]*0+list(range(1,60)),'mute'))):
        with pytest.raises(BackendError) as error:
            bad()
        assert error.value.code=='invalid_input'


def test_a_pin_limit_fails_one_chat_and_a_flood_stops_the_rest(world):
    engine,client=world
    ids=[cid(client.news),cid(client.barakholka),cid(client.mine)]
    client.fail[f.messages.ToggleDialogPinRequest]=type('PinnedDialogsTooMuchError',(Exception,),{})()
    result=execute(engine,go(engine.prepare_chat_setting(ids,'pin_chat')))
    assert [r['status'] for r in result['results']]==['failed']*3 and result['results'][0]['error']=='pin_limit' and not result['stopped']
    client.fail={f.account.UpdateNotifySettingsRequest:errors.FloodWaitError(request=None,capture=500)}
    result=execute(engine,go(engine.prepare_chat_setting(ids,'mute')))
    assert [r['status'] for r in result['results']]==['failed','not_attempted','not_attempted'] and result['stopped'] and result['results'][0]['error']=='rate_limited'


def test_deleting_dialogs_is_limited_to_private_chats_and_bots(world):
    engine,client=world
    draft=go(engine.prepare_delete_chats([cid(client.friend),cid(client.bot),cid(client.barakholka),-1009999999999],for_everyone=True))
    rows={row['chat_id']:row for row in draft['preview']['chats']}
    assert rows[cid(client.barakholka)]['reason']=='groups_and_channels_use_leave_chats' and draft['preview']['will_delete']==2 and draft['preview']['destructive'] and 'AND for the other person' in draft['preview']['effect']
    client.history_offsets=[100,0]
    result=execute(engine,draft)
    deleted=sent_requests(client,f.messages.DeleteHistoryRequest)
    assert result['done']==2 and len(deleted)==3 and all(r.revoke is True and r.just_clear is False and r.max_id==0 for r in deleted)
    mine_only=go(engine.prepare_delete_chats([cid(client.friend)]))
    assert 'only' in mine_only['preview']['effect']
    execute(engine,mine_only)
    assert sent_requests(client,f.messages.DeleteHistoryRequest)[-1].revoke is False
    with pytest.raises(BackendError) as error:
        go(engine.prepare_delete_chats([cid(client.news)]))
    assert error.value.code=='nothing_to_delete'


# ---- search, scheduled messages, polls --------------------------------------------------------------------

def test_search_across_all_own_chats(world):
    engine,client=world
    found=go(engine.search_all_messages('диван'))
    assert found['count']==1 and found['results'][0]=={**found['results'][0],'chat_id':cid(client.barakholka),'chat_title':'Барахолка Симферополь','message_id':77,'text':'Куплю диван срочно'}
    request=sent_requests(client,f.messages.SearchGlobalRequest)[0]
    assert request.q=='диван' and request.limit==30
    for bad in ('',' ','x'*201):
        with pytest.raises(BackendError):
            go(engine.search_all_messages(bad))


def test_scheduled_messages_can_be_listed_and_deleted_after_review(world):
    engine,client=world
    listing=go(engine.get_scheduled(cid(client.barakholka)))
    assert [row['message_id'] for row in listing['scheduled']]==[5,6] and listing['scheduled'][1]['has_media']
    draft=go(engine.prepare_delete_scheduled(cid(client.barakholka),[5]))
    assert draft['preview']['messages'][0]['text']=='Акция в пятницу' and engine.required_scope(draft['draft_id'])=='telegram.send'
    assert execute(engine,draft)['deleted'] and sent_requests(client,f.messages.DeleteScheduledMessagesRequest)[0].id==[5]
    with pytest.raises(BackendError) as error:
        go(engine.prepare_delete_scheduled(cid(client.barakholka),[5,99]))
    assert error.value.code=='message_not_found'


def test_polls_quizzes_and_their_validation(world):
    engine,client=world
    draft=go(engine.prepare_poll(cid(client.barakholka),'Когда удобнее?',['Утром','Вечером','Ночью'],multiple=True,anonymous=False))
    assert draft['preview']['choices']==['Утром','Вечером','Ночью'] and draft['preview']['multiple_answers'] and not draft['preview']['anonymous'] and engine.required_scope(draft['draft_id'])=='telegram.send'
    assert not sent_requests(client,f.messages.SendMediaRequest)
    execute(engine,draft)
    media=sent_requests(client,f.messages.SendMediaRequest)[0].media
    assert isinstance(media,t.InputMediaPoll) and media.poll.multiple_choice and media.poll.public_voters and media.poll.question.text=='Когда удобнее?'
    assert [a.text.text for a in media.poll.answers]==['Утром','Вечером','Ночью'] and [a.option for a in media.poll.answers]==[b'\x00',b'\x01',b'\x02'] and not media.poll.quiz
    quiz=go(engine.prepare_poll(None,'Столица?',['Москва','Киев'],quiz_answer=0))
    assert quiz['preview']['quiz_correct_option']=='Москва' and quiz['preview']['recipient']['type']=='saved_messages'
    execute(engine,quiz)
    asked=sent_requests(client,f.messages.SendMediaRequest)[-1].media
    assert asked.poll.quiz and asked.correct_answers==[0]
    for call in (lambda:go(engine.prepare_poll(None,'Один?',['да'])),lambda:go(engine.prepare_poll(None,'Дубли?',['да','Да'])),lambda:go(engine.prepare_poll(None,'',['а','б'])),
                 lambda:go(engine.prepare_poll(None,'Квиз?',['а','б'],quiz_answer=5)),lambda:go(engine.prepare_poll(None,'Квиз?',['а','б'],quiz_answer=0,multiple=True)),
                 lambda:go(engine.prepare_poll(None,'x',[str(i) for i in range(11)]))):
        with pytest.raises(BackendError) as error:
            call()
        assert error.value.code=='invalid_input'


# ---- chats the owner administers ---------------------------------------------------------------------------

def test_public_username_is_checked_previewed_and_applied(world):
    engine,client=world
    group=cid(client.admin_group)
    draft=go(engine.prepare_chat_admin_setting(group,'username','moya_gruppa'))
    assert draft['preview']['now']=='moya_gruppa' and 'anyone' in draft['preview']['note'].lower() and engine.required_scope(draft['draft_id'])=='telegram.manage'
    assert sent_requests(client,f.channels.CheckUsernameRequest)[0].username=='moya_gruppa'
    assert execute(engine,draft)['changed'] and sent_requests(client,f.channels.UpdateUsernameRequest)[0].username=='moya_gruppa'
    client.mine.username='mychannel'
    private=go(engine.prepare_chat_admin_setting(cid(client.mine),'username',''))
    assert private['preview']['now'] is None
    for value,code in (('taken_name','username_taken'),('ab','invalid_input'),('1abcdef','invalid_input')):
        with pytest.raises(BackendError) as error:
            go(engine.prepare_chat_admin_setting(group,'username',value))
        assert error.value.code==code
    with pytest.raises(BackendError) as error:
        go(engine.prepare_chat_admin_setting(cid(client.barakholka),'username','abcdefg'))
    assert error.value.code=='permission_denied'
    with pytest.raises(BackendError) as error:
        go(engine.prepare_chat_admin_setting(cid(client.chat),'username','abcdefg'))
    assert error.value.code=='unsupported_operation'


def test_slow_mode_join_approval_and_join_to_send(world):
    engine,client=world
    group,channel_id=cid(client.admin_group),cid(client.mine)
    execute(engine,go(engine.prepare_chat_admin_setting(group,'slow_mode',60)))
    assert sent_requests(client,f.channels.ToggleSlowModeRequest)[0].seconds==60
    execute(engine,go(engine.prepare_chat_admin_setting(group,'join_approval',True)))
    assert sent_requests(client,f.channels.ToggleJoinRequestRequest)[0].enabled is True
    execute(engine,go(engine.prepare_chat_admin_setting(group,'join_to_send',False)))
    assert sent_requests(client,f.channels.ToggleJoinToSendRequest)[0].enabled is False
    for call,code in ((lambda:go(engine.prepare_chat_admin_setting(group,'slow_mode',7)),'invalid_input'),(lambda:go(engine.prepare_chat_admin_setting(channel_id,'slow_mode',60)),'unsupported_operation'),
                      (lambda:go(engine.prepare_chat_admin_setting(channel_id,'join_to_send',True)),'unsupported_operation'),(lambda:go(engine.prepare_chat_admin_setting(group,'join_approval','yes')),'invalid_input'),
                      (lambda:go(engine.prepare_chat_admin_setting(group,'teleport',1)),'invalid_input'),(lambda:go(engine.prepare_chat_admin_setting(cid(client.friend),'slow_mode',60)),'invalid_input')):
        with pytest.raises(BackendError) as error:
            call()
        assert error.value.code==code


def test_member_permissions_show_was_and_now_and_send_the_full_rights(world):
    engine,client=world
    group=cid(client.admin_group)
    draft=go(engine.prepare_chat_admin_setting(group,'member_permissions',permissions={'send_polls':False,'pin_messages':True}))
    table=draft['preview']['permissions']
    assert draft['preview']['changed']==['send_polls','pin_messages'] and table['send_polls']=={'was_allowed':True,'now_allowed':False} and table['pin_messages']=={'was_allowed':False,'now_allowed':True}
    assert table['change_info']=={'was_allowed':False,'now_allowed':False}
    execute(engine,draft)
    rights=sent_requests(client,f.messages.EditChatDefaultBannedRightsRequest)[0].banned_rights
    assert rights.send_polls and not rights.pin_messages and rights.change_info and not rights.send_messages and rights.until_date in (0,None)
    for bad in ({'send_messages':True,'change_info':False},{'fly':True},{'send_messages':'yes'},{}):
        with pytest.raises(BackendError) as error:
            go(engine.prepare_chat_admin_setting(group,'member_permissions',permissions=bad))
        assert error.value.code in ('invalid_input','no_change')
    with pytest.raises(BackendError) as error:
        go(engine.prepare_chat_admin_setting(cid(client.helper),'member_permissions',permissions={'send_polls':False}))
    assert error.value.code=='permission_denied'


def test_chat_photo_needs_a_staged_image(world):
    engine,client=world
    image=stage(engine,b'jpeg bytes','лого.jpg','image/jpeg')
    draft=go(engine.prepare_chat_admin_setting(cid(client.admin_group),'photo',file_id=image['file_id']))
    assert draft['preview']['media'][0]['sha256']==image['sha256']
    execute(engine,draft)
    assert isinstance(sent_requests(client,f.channels.EditPhotoRequest)[0].photo,t.InputChatUploadedPhoto)
    document=stage(engine,b'%PDF','x.pdf','application/pdf')
    with pytest.raises(BackendError) as error:
        go(engine.prepare_chat_admin_setting(cid(client.admin_group),'photo',file_id=document['file_id']))
    assert error.value.code=='unsupported_media'


def test_invite_links_are_listed_created_with_limits_and_revoked(world):
    engine,client=world
    group=cid(client.admin_group)
    listing=go(engine.list_invite_links(group))
    assert listing['links'][0]['link']=='https://t.me/+AAAAA11111' and listing['links'][0]['joined']==3 and listing['links'][0]['permanent']
    draft=go(engine.prepare_invite_link(group,'create',title='Реклама',expire_days=7,usage_limit=50))
    assert draft['preview']['expires_in_days']==7 and draft['preview']['usage_limit']==50 and 'join' in draft['preview']['warning']
    result=execute(engine,draft)
    request=sent_requests(client,f.messages.ExportChatInviteRequest)[0]
    assert result['link']=='https://t.me/+NEWLINK12345' and request.usage_limit==50 and request.title=='Реклама' and request.expire_date is not None
    revoke=go(engine.prepare_invite_link(group,'revoke',link='https://t.me/+NEWLINK12345'))
    assert revoke['preview']['destructive']
    execute(engine,revoke)
    assert sent_requests(client,f.messages.EditExportedChatInviteRequest)[0].revoked is True
    for call in (lambda:go(engine.prepare_invite_link(group,'create',request_needed=True,usage_limit=5)),lambda:go(engine.prepare_invite_link(group,'create',expire_days=0)),
                 lambda:go(engine.prepare_invite_link(group,'revoke',link='https://evil.example/x')),lambda:go(engine.prepare_invite_link(group,'burn'))):
        with pytest.raises(BackendError) as error:
            call()
        assert error.value.code=='invalid_input'
    with pytest.raises(BackendError) as error:
        go(engine.prepare_invite_link(cid(client.barakholka),'create'))
    assert error.value.code=='permission_denied'
    assert go(engine.prepare_invite_link(cid(client.helper),'create',request_needed=True))['preview']['request_needed']


def test_join_requests_are_listed_and_decided_by_name(world):
    engine,client=world
    group=cid(client.helper)
    listing=go(engine.list_join_requests(group))
    assert [r['user_id'] for r in listing['requests']]==[900,901] and listing['requests'][0]['name']=='Новичок' and listing['requests'][0]['about']=='Хочу в чат'
    draft=go(engine.prepare_join_requests_decision(group,[900],True))
    assert draft['preview']['people']==[{'user_id':900,'name':'Новичок','username':'newbie'}] and draft['preview']['decision']=='approve' and not draft['preview']['destructive']
    result=execute(engine,draft)
    hide=sent_requests(client,f.messages.HideChatJoinRequestRequest)[0]
    assert result['decision']=='approved' and result['done']==1 and hide.approved is True and hide.user_id.user_id==900
    decline=go(engine.prepare_join_requests_decision(group,[900,901],False))
    assert decline['preview']['destructive']
    execute(engine,decline)
    assert [r.approved for r in sent_requests(client,f.messages.HideChatJoinRequestRequest)[1:]]==[False,False]
    with pytest.raises(BackendError) as error:
        go(engine.prepare_join_requests_decision(group,[12345],True))
    assert error.value.code=='no_such_request'
    with pytest.raises(BackendError) as error:
        go(engine.list_join_requests(cid(client.barakholka)))
    assert error.value.code=='permission_denied'


def test_forum_topics_can_be_renamed_closed_reopened_and_deleted(world):
    engine,client=world
    forum=cid(client.forum)
    rename=go(engine.prepare_topic_change(forum,7,'rename','Продажи'))
    assert rename['preview']['topic']=={'id':7,'title':'Объявления'} and rename['preview']['new_title']=='Продажи'
    execute(engine,rename)
    assert sent_requests(client,f.messages.EditForumTopicRequest)[-1].title=='Продажи'
    execute(engine,go(engine.prepare_topic_change(forum,7,'close')))
    assert sent_requests(client,f.messages.EditForumTopicRequest)[-1].closed is True
    execute(engine,go(engine.prepare_topic_change(forum,7,'reopen')))
    assert sent_requests(client,f.messages.EditForumTopicRequest)[-1].closed is False
    delete=go(engine.prepare_topic_change(forum,7,'delete'))
    assert delete['preview']['destructive'] and 'ALL its messages' in delete['preview']['effect']
    execute(engine,delete)
    assert sent_requests(client,f.messages.DeleteTopicHistoryRequest)[0].top_msg_id==7
    for call,code in ((lambda:go(engine.prepare_topic_change(cid(client.admin_group),7,'close')),'topics_unavailable'),(lambda:go(engine.prepare_topic_change(forum,0,'close')),'invalid_input'),
                      (lambda:go(engine.prepare_topic_change(forum,7,'explode')),'invalid_input'),(lambda:go(engine.prepare_topic_change(forum,7,'rename')),'invalid_input')):
        with pytest.raises(BackendError) as error:
            call()
        assert error.value.code==code


def test_admin_log_summarises_actions(world):
    engine,client=world
    log=go(engine.get_admin_log(cid(client.admin_group),limit=10))
    assert log['events'][0]['action']=='ToggleSlowMode' and log['events'][0]['by']=='Владелец' and sent_requests(client,f.channels.GetAdminLogRequest)[0].limit==10
    for chat_id,code in ((cid(client.chat),'unsupported_operation'),(cid(client.barakholka),'permission_denied')):
        with pytest.raises(BackendError) as error:
            go(engine.get_admin_log(chat_id))
        assert error.value.code==code


# ---- deleting an owned chat, blocking ---------------------------------------------------------------------

def test_deleting_an_owned_chat_needs_the_exact_title_and_ownership(world):
    engine,client=world
    client.filters.append(folder(9,'Мои',client.mine))
    with pytest.raises(BackendError) as error:
        go(engine.prepare_delete_chat_for_all(cid(client.mine),'не то'))
    assert error.value.code=='confirm_title_mismatch' and error.value.diagnostics['title']=='Мой канал'
    with pytest.raises(BackendError) as error:
        go(engine.prepare_delete_chat_for_all(cid(client.barakholka),'Барахолка Симферополь'))
    assert error.value.code=='permission_denied'
    with pytest.raises(BackendError) as error:
        go(engine.prepare_delete_chat_for_all(cid(client.friend),'Друг'))
    assert error.value.code=='invalid_input'
    draft=go(engine.prepare_delete_chat_for_all(cid(client.mine),'Мой канал'))
    assert draft['preview']['irreversible'] and draft['preview']['destructive'] and not sent_requests(client,f.channels.DeleteChannelRequest) and engine.required_scope(draft['draft_id'])=='telegram.manage'
    result=execute(engine,draft)
    assert result['deleted'] and sent_requests(client,f.channels.DeleteChannelRequest)[0].channel.channel_id==client.mine.id and result['removed_from_folders']==1
    client.mine.title='Другое имя'
    client.dialogs=[dialog(e) if e is not client.mine else dialog(client.mine) for e in [d.entity for d in client.dialogs]]
    again=go(engine.prepare_delete_chat_for_all(cid(client.mine),'Другое имя'))
    client.mine.title='Опять другое'
    with pytest.raises(BackendError) as error:
        execute(engine,again)
    assert error.value.code=='confirm_title_mismatch' and len(sent_requests(client,f.channels.DeleteChannelRequest))==1


def test_block_and_unblock_people_and_bots_only(world):
    engine,client=world
    by_id=go(engine.prepare_block_user(cid(client.friend)))
    assert by_id['preview']['operation']=='block' and by_id['preview']['reversible'] and by_id['preview']['user']['name']=='Друг'
    execute(engine,by_id)
    assert isinstance(sent_requests(client,f.contacts.BlockRequest)[0].id,t.InputPeerUser)
    by_name=go(engine.prepare_block_user('@some_person',block=False))
    assert by_name['preview']['operation']=='unblock'
    execute(engine,by_name)
    assert sent_requests(client,f.contacts.UnblockRequest)[0].id.user_id==client.someone.id
    for call,code in ((lambda:go(engine.prepare_block_user(cid(client.barakholka))),'invalid_input'),(lambda:go(engine.prepare_block_user('@nobody_here')),'user_not_accessible'),
                      (lambda:go(engine.prepare_block_user(424242)),'chat_not_found'),(lambda:go(engine.prepare_block_user('no good!')),'invalid_input'),(lambda:go(engine.prepare_block_user(True)),'invalid_input')):
        with pytest.raises(BackendError) as error:
            call()
        assert error.value.code==code


def test_audit_names_the_action_but_never_the_text(world):
    engine,client=world
    execute(engine,go(engine.prepare_poll(cid(client.barakholka),'Секретный вопрос?',['Да','Нет'])))
    execute(engine,go(engine.prepare_chat_setting([cid(client.news)],'archive')))
    entries=engine.audit.entries()
    assert {'send_poll','chat_setting'}<={e['action'] for e in entries} and 'Секретный' not in json.dumps(entries,ensure_ascii=False)


# ---- MCP surface -------------------------------------------------------------------------------------------

@pytest.fixture
def web(tmp_path,oauth,monkeypatch):
    client=ChatClient()
    engine=ExtendedBackend(client=client,allow_send=True,output_dir=tmp_path/'output',drafts_db=tmp_path/'drafts.sqlite',bot_policy=BotPolicy(None),allow_botfather=False,limits=Limits({'TG_CAMPAIGN_MIN_DELAY':'0'}))
    owner,_,_=oauth
    monkeypatch.setenv('TG_DATA_DIR',str(tmp_path))
    with TestClient(create_app(engine,owner),base_url=owner.public_url) as app:
        yield app,owner,engine,client


def tool(app,owner,scopes,name,arguments):
    return rpc(app,bearer(owner,scopes),'tools/call',{'name':name,'arguments':arguments}).json()['result']['structuredContent']


def test_new_chat_tools_are_listed_and_scoped_correctly(web):
    app,owner,engine,client=web
    tools={x['name']:x for x in rpc(app,bearer(owner,['telegram.read']),'tools/list').json()['result']['tools']}
    assert len(tools)==108
    names=['telegram_search_all_messages','telegram_get_scheduled','telegram_prepare_delete_scheduled','telegram_prepare_poll','telegram_prepare_chat_setting','telegram_prepare_delete_chats',
           'telegram_prepare_chat_admin_setting','telegram_list_invite_links','telegram_prepare_invite_link','telegram_list_join_requests','telegram_prepare_join_requests_decision',
           'telegram_prepare_topic_change','telegram_get_admin_log','telegram_prepare_delete_chat_for_all','telegram_prepare_block_user']
    assert all(name in tools and tools[name]['annotations']['readOnlyHint'] is True for name in names)
    send=['telegram.read','telegram.send']
    manage=send+['telegram.manage']
    poll=tool(app,owner,send,'telegram_prepare_poll',{'chat_id':cid(client.barakholka),'question':'Идём?','options':['да','нет']})
    args={'draft_id':poll['draft_id'],'confirmation_hash':poll['confirmation_hash']}
    assert tool(app,owner,['telegram.read'],'telegram_execute_action',args)['error']=='insufficient_scope'
    assert tool(app,owner,send,'telegram_execute_action',args)['sent'] is True
    setting=tool(app,owner,send,'telegram_prepare_chat_setting',{'chat_ids':[cid(client.news)],'setting':'mute','hours':2})
    args={'draft_id':setting['draft_id'],'confirmation_hash':setting['confirmation_hash']}
    assert tool(app,owner,send,'telegram_execute_action',args)['error']=='insufficient_scope' and tool(app,owner,send,'telegram_execute_management_action',args)['error']=='insufficient_scope'
    done=tool(app,owner,manage,'telegram_execute_management_action',args)
    assert done['done']==1 and not done['stopped'] and len(sent_requests(client,f.account.UpdateNotifySettingsRequest))==1
    assert tool(app,owner,manage,'telegram_search_all_messages',{'query':'диван'})['count']==1
    assert tool(app,owner,manage,'telegram_prepare_chat_admin_setting',{'chat_id':cid(client.admin_group),'setting':'member_permissions','permissions':{'send_polls':False}})['preview']['changed']==['send_polls']
    assert tool(app,owner,manage,'telegram_prepare_block_user',{'user':'@some_person'})['preview']['operation']=='block'
