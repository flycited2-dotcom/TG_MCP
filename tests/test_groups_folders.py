"""Finding, joining and leaving groups, chat folders and slow capped campaigns on a fabricated Telethon client."""
import asyncio
import json
import sqlite3
from datetime import datetime,timezone
from types import SimpleNamespace as NS

import pytest
from starlette.testclient import TestClient
from telethon import errors,utils
from telethon.tl import functions as f,types as t

import jobs
from bot_interaction import BotPolicy
from chat_discovery import parse_target
from extended_backend import ExtendedBackend
from jobs import JobStore,Limits
from telegram_backend import BackendError
from test_server import bearer,rpc
from test_oauth_owner import oauth
from server import create_app

NOW=datetime.now(timezone.utc)


def channel(identifier,title,**flags):
    return t.Channel(id=identifier,title=title,photo=t.ChatPhotoEmpty(),date=NOW,access_hash=identifier+7,**flags)


def cid(entity):
    return utils.get_peer_id(entity)


def dialog(entity,archived=False,pinned=False):
    is_channel=type(entity).__name__=='Channel'
    return NS(id=cid(entity),entity=entity,name=getattr(entity,'title',None),is_channel=is_channel,is_group=bool(getattr(entity,'megagroup',False)) or type(entity).__name__=='Chat',archived=archived,pinned=pinned)


def folder(identifier,title,*entities,**flags):
    return t.DialogFilter(id=identifier,title=t.TextWithEntities(text=title,entities=[]),pinned_peers=[],include_peers=[utils.get_input_peer(e) for e in entities],exclude_peers=[],**flags)


class GroupClient:
    def __init__(self):
        self.requests=[]
        self.owner=t.User(id=999,access_hash=1,first_name='Владелец',username='owner')
        self.barakholka=channel(11,'Барахолка Симферополь',megagroup=True,username='barahol_simf',participants_count=5400)
        self.barakholka2=channel(12,'Барахолка Крым',megagroup=True,username='barahol_krym',participants_count=12000)
        self.news=channel(13,'Новости города',broadcast=True,username='city_news',participants_count=90000)
        self.muted=channel(14,'Закрытая барахолка',megagroup=True,default_banned_rights=t.ChatBannedRights(until_date=None,send_messages=True),participants_count=300)
        self.mine=channel(15,'Мой канал',broadcast=True,creator=True,participants_count=10)
        self.chat=t.Chat(id=21,title='Соседи',photo=t.ChatPhotoEmpty(),participants_count=40,date=NOW,version=1)
        self.friend=t.User(id=100,access_hash=2,first_name='Друг')
        self.someone=t.User(id=777,access_hash=3,first_name='Человек',username='some_person')
        self.joinable=channel(31,'Реклама Крым',megagroup=True,username='ads_krym',participants_count=800,join_request=True,left=True)
        self.free=channel(32,'Объявления Ялта',megagroup=True,username='yalta_ads',participants_count=2200,left=True,default_banned_rights=t.ChatBannedRights(until_date=None,send_messages=True))
        self.broken=channel(33,'Битая',megagroup=True,username='broken_group',left=True)
        self.dialogs=[dialog(e) for e in (self.barakholka,self.barakholka2,self.news,self.muted,self.mine,self.chat,self.friend)]
        self.dialogs[1].archived=True
        self.public={e.username:e for e in (self.barakholka,self.barakholka2,self.news,self.joinable,self.free,self.broken,self.someone)}
        self.full={e.id:NS(about=about,slowmode_seconds=slow,participants_count=e.participants_count,pinned_msg_id=pin,linked_chat_id=None) for e,about,slow,pin in
                   ((self.barakholka,'Продаём и покупаем. Реклама запрещена.',30,5),(self.joinable,'Реклама по вторникам',None,None),(self.free,'Объявления Ялты',None,6),(self.news,'Новости',None,None))}
        self.pinned={5:'Правила: реклама только через админа.',6:'Разрешено размещать объявления'}
        self.invites={'GOODHASH12345':NS(title='Закрытый клуб',participants_count=500,request_needed=True,about='Клуб',broadcast=False,megagroup=True,public=False)}
        self.filters=[folder(2,'Реклама',self.barakholka)]
        self.join_errors={}
        self.invite_errors={}
        self.leave_errors={}
        self.send_errors={}
        self.filter_error=None
        self.sent=0

    def is_connected(self): return True
    async def connect(self): pass
    async def disconnect(self): pass
    async def is_user_authorized(self): return True
    async def get_me(self): return self.owner
    async def iter_dialogs(self):
        for item in self.dialogs:
            yield item
    async def get_entity(self,name):
        key=name.lstrip('@')
        if key not in self.public:
            raise errors.UsernameNotOccupiedError(request=None)
        return self.public[key]
    async def get_input_entity(self,entity):
        if isinstance(entity,str):
            entity=await self.get_entity(entity)
        if isinstance(entity,int):
            entity=next(d.entity for d in self.dialogs if d.id==entity)
        return utils.get_input_peer(entity)
    async def get_messages(self,entity,ids):
        return NS(message=self.pinned.get(ids))
    async def upload_file(self,path,file_name):
        return t.InputFile(id=200,parts=1,name=file_name,md5_checksum='')

    async def __call__(self,request):
        self.requests.append(request)
        bytes(request)
        if isinstance(request,f.contacts.SearchRequest):
            return NS(chats=[self.joinable,self.free,self.news,self.barakholka,self.someone],users=[],results=[],my_results=[])
        if isinstance(request,f.channels.GetFullChannelRequest):
            return NS(full_chat=self.full[request.channel.channel_id])
        if isinstance(request,f.messages.CheckChatInviteRequest):
            if request.hash not in self.invites:
                raise errors.InviteHashInvalidError(request=None)
            return self.invites[request.hash]
        if isinstance(request,f.messages.GetDialogFiltersRequest):
            return NS(filters=[t.DialogFilterDefault()]+list(self.filters),tags_enabled=False)
        if isinstance(request,f.messages.UpdateDialogFilterRequest):
            if self.filter_error:
                raise self.filter_error
            self.filters=[x for x in self.filters if x.id!=request.id]
            if request.filter is not None:
                self.filters.append(request.filter)
            return True
        if isinstance(request,f.channels.JoinChannelRequest):
            if request.channel.channel_id in self.join_errors:
                raise self.join_errors[request.channel.channel_id]
            entity=next(e for e in self.public.values() if getattr(e,'id',None)==request.channel.channel_id)
            return NS(chats=[entity],updates=[])
        if isinstance(request,f.messages.ImportChatInviteRequest):
            if request.hash in self.invite_errors:
                raise self.invite_errors[request.hash]
            return NS(chats=[self.barakholka2],updates=[])
        if isinstance(request,f.channels.LeaveChannelRequest):
            error=self.leave_errors.get(request.channel.channel_id)
            if error:
                raise error
            return NS(updates=[])
        if isinstance(request,(f.messages.SendMessageRequest,f.messages.SendMediaRequest)):
            try:
                key=utils.get_peer_id(request.peer)
            except TypeError:
                key=None
            if key in self.send_errors:
                raise self.send_errors[key]
            self.sent+=1
            return t.Updates(updates=[t.UpdateMessageID(id=1000+self.sent,random_id=request.random_id)],users=[],chats=[],date=NOW,seq=1)
        return NS(updates=[])


@pytest.fixture(autouse=True)
def instant(monkeypatch):
    async def quick(self,seconds):
        await asyncio.sleep(0)
    monkeypatch.setattr(jobs.Jobs,'_sleep',quick)


def build(tmp_path,**limits):
    client=GroupClient()
    engine=ExtendedBackend(client=client,allow_send=True,output_dir=tmp_path/'output',drafts_db=tmp_path/'drafts.sqlite',bot_policy=BotPolicy(None),allow_botfather=False,
                           limits=Limits({'TG_CAMPAIGN_MIN_DELAY':'0',**{key:str(value) for key,value in limits.items()}}))
    return engine,client


@pytest.fixture
def world(tmp_path):
    return build(tmp_path)


def go(coroutine):
    return asyncio.run(coroutine)


async def do(engine,draft,wait=5):
    result=await engine.execute_action(draft['draft_id'],draft['confirmation_hash'])
    return await engine.collect_job(result,wait)


def sent_requests(client,kind):
    return [r for r in client.requests if isinstance(r,kind)]


# ---- targets, own chats and public search -----------------------------------------------------------------------

@pytest.mark.parametrize('value,expected',[('@barahol_simf',{'kind':'username','name':'barahol_simf'}),('barahol_simf',{'kind':'username','name':'barahol_simf'}),
    ('https://t.me/barahol_simf',{'kind':'username','name':'barahol_simf'}),('t.me/barahol_simf/123?single',{'kind':'username','name':'barahol_simf'}),
    ('https://t.me/+AbCdEf123456',{'kind':'invite','hash':'AbCdEf123456'}),('https://t.me/joinchat/AbCdEf123456',{'kind':'invite','hash':'AbCdEf123456'}),
    ('tg://join?invite=AbCdEf123456',{'kind':'invite','hash':'AbCdEf123456'}),('tg://resolve?domain=barahol_simf',{'kind':'username','name':'barahol_simf'})])
def test_targets_are_parsed(value,expected):
    assert parse_target(value)==expected


@pytest.mark.parametrize('value',['','  ','12345','https://t.me/c/123/4','https://t.me/addlist/abcde','https://example.com/barahol','a','@ab','https://t.me/+','x'*400,None,5])
def test_bad_targets_are_refused(value):
    with pytest.raises(BackendError) as error:
        parse_target(value)
    assert error.value.code=='invalid_input'


def test_list_chats_filters_by_name_kind_and_folder(world):
    engine,client=world
    everything=go(engine.list_chats())
    assert everything['count']==7 and everything['chats'][0]['chat_id']==cid(client.barakholka) and everything['chats'][0]['type']=='supergroup'
    named=go(engine.list_chats(query='барахолка'))['chats']
    assert [row['title'] for row in named]==['Барахолка Симферополь','Барахолка Крым','Закрытая барахолка']
    assert named[1]['archived'] is True and named[0]['members']==5400
    assert [row['type'] for row in go(engine.list_chats(kind='channel'))['chats']]==['channel','channel']
    assert {row['type'] for row in go(engine.list_chats(kind='groups_and_channels'))['chats']}=={'supergroup','channel','group'}
    assert [row['chat_id'] for row in go(engine.list_chats(folder='Реклама'))['chats']]==[cid(client.barakholka)]
    assert len(go(engine.list_chats(limit=2))['chats'])==2
    with pytest.raises(BackendError) as error:
        go(engine.list_chats(kind='everything'))
    assert error.value.code=='invalid_input'
    with pytest.raises(BackendError) as error:
        go(engine.list_chats(folder='Нет такой'))
    assert error.value.code=='folder_not_found' and error.value.diagnostics['folders'][0]['title']=='Реклама'


def test_public_search_returns_only_chats_with_join_handles(world):
    engine,client=world
    found=go(engine.search_public_chats('барахолка'))
    assert [row['title'] for row in found['results']]==['Реклама Крым','Объявления Ялта','Новости города','Барахолка Симферополь']
    assert found['results'][0]['target']=='@ads_krym' and found['results'][0]['member'] is False and found['results'][3]['member'] is True
    assert found['results'][0]['join_request'] is True and 'untrusted' in found['data_trust']
    assert [row['type'] for row in go(engine.search_public_chats('x',kind='channel'))['results']]==['channel']
    assert 'channel' not in {row['type'] for row in go(engine.search_public_chats('x',kind='group'))['results']}
    assert not sent_requests(client,f.channels.JoinChannelRequest)
    with pytest.raises(BackendError):
        go(engine.search_public_chats(''))


def test_inspect_chat_reports_rules_slow_mode_and_approval(world):
    engine,client=world
    info=go(engine.inspect_chat('@barahol_simf'))
    assert info['members']==5400 and info['slow_mode_seconds']==30 and info['pinned_message_text'].startswith('Правила') and info['member'] is True
    assert 'slow_mode_30s' in info['warnings'] and 'rules_may_forbid_ads' in info['warnings'] and 'rules_notice' in info
    request_chat=go(engine.inspect_chat('https://t.me/ads_krym'))
    assert request_chat['requires_approval'] is True and 'join_request_required' in request_chat['warnings'] and request_chat['member'] is False
    closed=go(engine.inspect_chat('@yalta_ads'))
    assert 'members_cannot_post' in closed['warnings'] and 'rules_may_forbid_ads' not in closed['warnings']
    assert 'channel_posting_needs_admin_rights' in go(engine.inspect_chat('@city_news'))['warnings']
    invite=go(engine.inspect_chat('https://t.me/+GOODHASH12345'))
    assert invite['title']=='Закрытый клуб' and invite['requires_approval'] and 'join_request_required' in invite['warnings'] and invite['type']=='supergroup'
    for target,code in (('https://t.me/+BADHASH12345','invite_invalid'),('@nobody_here','chat_not_found'),('@some_person','not_a_chat')):
        with pytest.raises(BackendError) as error:
            go(engine.inspect_chat(target))
        assert error.value.code==code
    assert not sent_requests(client,f.channels.JoinChannelRequest)


# ---- folders ----------------------------------------------------------------------------------------------------

def test_folders_are_listed_with_their_chats(world):
    engine,client=world
    listing=go(engine.list_folders())
    assert listing['folders']==[{'id':2,'title':'Реклама','emoticon':None,'chat_count':1,'shared_folder':False,'also_includes_by_rule':[]}]
    shown=go(engine.get_folder('реклама'))
    assert shown['folder']['id']==2 and shown['chats'][0]['title']=='Барахолка Симферополь' and shown['chats'][0]['chat_id']==cid(client.barakholka)
    assert go(engine.get_folder(2))['count']==1


def test_create_folder_with_chats_has_a_preview_and_a_real_filter(world):
    engine,client=world
    ids=[cid(client.barakholka2),cid(client.muted)]
    async def scenario():
        draft=await engine.prepare_folder_change('create',title='Барахолки',chat_ids=ids)
        assert engine.required_scope(draft['draft_id'])=='telegram.manage' and not sent_requests(client,f.messages.UpdateDialogFilterRequest)
        assert [c['title'] for c in draft['preview']['added']]==['Барахолка Крым','Закрытая барахолка'] and draft['preview']['chats_before']==[]
        return draft,await do(engine,draft)
    draft,result=go(scenario())
    request=sent_requests(client,f.messages.UpdateDialogFilterRequest)[0]
    assert request.id==3 and request.filter.title.text=='Барахолки' and [utils.get_peer_id(p) for p in request.filter.include_peers]==ids
    assert result['changed'] and result['folder']=={'id':3,'title':'Барахолки'}
    assert [x['title'] for x in go(engine.list_folders())['folders']]==['Реклама','Барахолки']


def test_add_and_remove_chats_keep_the_rest_of_the_folder(world):
    engine,client=world
    async def scenario():
        add=await engine.prepare_folder_change('add_chats',folder='Реклама',chat_ids=[cid(client.barakholka2),cid(client.barakholka)])
        assert [c['chat_id'] for c in add['preview']['added']]==[cid(client.barakholka2)] and len(add['preview']['chats_after'])==2
        await do(engine,add)
        assert [utils.get_peer_id(p) for p in client.filters[0].include_peers]==[cid(client.barakholka),cid(client.barakholka2)]
        remove=await engine.prepare_folder_change('remove_chats',folder=2,chat_ids=[cid(client.barakholka)])
        assert remove['preview']['destructive'] and [c['chat_id'] for c in remove['preview']['removed']]==[cid(client.barakholka)]
        await do(engine,remove)
        return [utils.get_peer_id(p) for p in client.filters[0].include_peers]
    assert go(scenario())==[cid(client.barakholka2)] if (client:=world[1]) else None


def test_folder_change_errors_are_specific(world):
    engine,client=world
    async def expect(code,*args,**kwargs):
        with pytest.raises(BackendError) as error:
            await engine.prepare_folder_change(*args,**kwargs)
        assert error.value.code==code
        return error.value
    async def scenario():
        await expect('no_change','add_chats',folder='Реклама',chat_ids=[cid(client.barakholka)])
        await expect('no_change','remove_chats',folder='Реклама',chat_ids=[cid(client.news)])
        await expect('chat_not_found','add_chats',folder='Реклама',chat_ids=[-1009999999999])
        await expect('invalid_input','add_chats',folder='Реклама',chat_ids=[])
        await expect('invalid_input','frobnicate',folder='Реклама')
        await expect('invalid_input','create',title='')
        await expect('invalid_input','create',title='x'*40)
        await expect('folder_exists','create',title='реклама')
        await expect('no_change','rename',folder=2,title='Реклама')
        client.filters.append(folder(4,'Другая'))
        await expect('folder_exists','rename',folder=2,title='другая')
        client.filters.pop()
        problem=await expect('folder_not_found','delete',folder='Нет')
        assert problem.diagnostics['folders']==[{'id':2,'title':'Реклама'}]
        client.filters.append(folder(5,'Реклама'))
        await expect('ambiguous_folder','delete',folder='Реклама')
        client.filters.append(t.DialogFilterChatlist(id=6,title=t.TextWithEntities(text='Общая',entities=[]),pinned_peers=[],include_peers=[]))
        await expect('unsupported_operation','rename',folder='Общая',title='Другая')
    go(scenario())


def test_rename_and_delete_and_telegram_folder_limits(world):
    engine,client=world
    async def scenario():
        rename=await engine.prepare_folder_change('rename',folder='Реклама',title='Рассылки')
        assert rename['preview']['new_title']=='Рассылки'
        await do(engine,rename)
        assert client.filters[0].title.text=='Рассылки' and len(client.filters[0].include_peers)==1
        client.filter_error=type('FilterIncludeTooMuchError',(Exception,),{})()
        failing=await engine.prepare_folder_change('add_chats',folder='Рассылки',chat_ids=[cid(client.news)])
        with pytest.raises(BackendError) as error:
            await do(engine,failing)
        assert error.value.code=='folder_limit'
        client.filter_error=None
        assert (await do(engine,failing))['changed']
        removal=await engine.prepare_folder_change('delete',folder='Рассылки')
        assert removal['preview']['destructive'] and removal['preview']['removed']
        await do(engine,removal)
        return client.filters
    assert go(scenario())==[]


# ---- joining ----------------------------------------------------------------------------------------------------

def test_join_is_previewed_then_runs_as_a_paced_job(world):
    engine,client=world
    async def scenario():
        draft=await engine.prepare_join_chats(['@ads_krym','@barahol_simf','https://t.me/+GOODHASH12345','@nobody_here'])
        rows={row['target']:row for row in draft['preview']['chats']}
        assert rows['@ads_krym']['status']=='will_join' and rows['@ads_krym']['requires_approval'] and rows['@barahol_simf']['status']=='already_member'
        assert rows['@nobody_here']['status']=='invalid' and draft['preview']['will_join']==2 and 'hash' not in json.dumps(draft['preview'])
        assert engine.required_scope(draft['draft_id'])=='telegram.manage' and not sent_requests(client,f.channels.JoinChannelRequest)
        return await do(engine,draft)
    result=go(scenario())
    assert result['started'] and result['job']['state']=='done' and result['job']['counts']=={'joined':2}
    assert len(sent_requests(client,f.channels.JoinChannelRequest))==1 and len(sent_requests(client,f.messages.ImportChatInviteRequest))==1
    assert result['job']['steps'][0]['chat_id']==cid(client.joinable)
    assert engine.limits_status()['used_last_24h']['joins']==2 and engine.limits_status()['remaining_24h']['joins']==8


def test_join_outcomes_requests_existing_membership_and_bad_links(world):
    engine,client=world
    client.join_errors[client.joinable.id]=errors.InviteRequestSentError(request=None)
    client.join_errors[client.free.id]=errors.UserAlreadyParticipantError(request=None)
    client.invite_errors['GOODHASH12345']=errors.InviteHashExpiredError(request=None)
    client.join_errors[client.broken.id]=errors.ChannelPrivateError(request=None)
    client.public['broken_group']=client.broken
    client.full[client.broken.id]=NS(about=None,slowmode_seconds=None,participants_count=None,pinned_msg_id=None,linked_chat_id=None)
    async def scenario():
        draft=await engine.prepare_join_chats(['@ads_krym','@yalta_ads','https://t.me/+GOODHASH12345','@broken_group'])
        return await do(engine,draft)
    steps=go(scenario())['job']['steps']
    assert [s['status'] for s in steps]==['request_sent','already_member','failed','failed'] and steps[2]['error']=='invite_invalid' and steps[3]['error']=='not_found_or_private'
    assert engine.limits_status()['used_last_24h']['joins']==1


def test_one_unreadable_chat_does_not_spoil_the_whole_join_list(world):
    engine,client=world
    client.public['broken_group']=client.broken
    draft=go(engine.prepare_join_chats(['@broken_group','@ads_krym']))
    rows={row['target']:row for row in draft['preview']['chats']}
    assert rows['@broken_group']['status']=='invalid' and rows['@broken_group']['reason']=='telegram_error' and rows['@ads_krym']['status']=='will_join' and draft['preview']['will_join']==1


def test_join_stops_when_telegram_says_too_many_chats_or_flood(world):
    engine,client=world
    client.join_errors[client.joinable.id]=errors.ChannelsTooMuchError(request=None)
    async def scenario():
        return await do(engine,await engine.prepare_join_chats(['@ads_krym','@yalta_ads']))
    job=go(scenario())['job']
    assert job['state']=='stopped' and job['steps'][0]['error']=='too_many_chats' and job['steps'][1]['status']=='not_attempted' and 'Remaining steps were not attempted' in job['note']
    client.join_errors={client.joinable.id:errors.FloodWaitError(request=None,capture=500)}
    job=go(scenario_stop(engine))['job']
    assert job['state']=='stopped' and job['steps'][0]['error']=='rate_limited' and job['steps'][1]['status']=='not_attempted' and 'Telegram asks to wait 500' in job['note']


async def scenario_stop(engine):
    return await do(engine,await engine.prepare_join_chats(['@ads_krym','@yalta_ads']))


def test_join_limits_come_from_the_server_and_quota_blocks_the_list(tmp_path):
    engine,client=build(tmp_path,TG_JOIN_DAILY_LIMIT=1)
    with pytest.raises(BackendError) as error:
        go(engine.prepare_join_chats(['@ads_krym','@yalta_ads']))
    assert error.value.code=='daily_limit_reached' and error.value.diagnostics=={'used_24h':0,'limit_24h':1,'requested':2}
    go(do_one(engine,'@ads_krym'))
    with pytest.raises(BackendError) as error:
        go(engine.prepare_join_chats(['@yalta_ads']))
    assert error.value.code=='daily_limit_reached' and error.value.diagnostics['used_24h']==1
    with pytest.raises(BackendError) as error:
        go(engine.prepare_join_chats(['@barahol_simf']))
    assert error.value.code=='nothing_to_join'
    with pytest.raises(BackendError):
        go(engine.prepare_join_chats(['@a_chat_']*11))
    with pytest.raises(BackendError):
        go(engine.prepare_join_chats([]))


async def do_one(engine,target):
    return await do(engine,await engine.prepare_join_chats([target]))


def test_only_one_job_runs_at_a_time_and_cancel_skips_the_rest(world):
    engine,client=world
    async def scenario():
        first=await engine.prepare_join_chats(['@ads_krym','@yalta_ads'])
        second=await engine.prepare_join_chats(['@ads_krym','@yalta_ads'])
        started=await engine.execute_action(first['draft_id'],first['confirmation_hash'])
        with pytest.raises(BackendError) as error:
            await engine.execute_action(second['draft_id'],second['confirmation_hash'])
        assert error.value.code=='job_already_running' and error.value.diagnostics['job_id']==started['job_id']
        cancel=engine.job_cancel(started['job_id'])
        assert cancel['cancelled'] is True
        final=await engine.job_wait(started['job_id'],5)
        return final
    final=go(scenario())
    assert final['state']=='cancelled' and final['counts']=={'cancelled':2} and not sent_requests(client,f.channels.JoinChannelRequest)
    assert go(job_cancel_again(engine,final))['cancelled'] is False


async def job_cancel_again(engine,final):
    return engine.job_cancel(final['job_id'])


def test_a_restart_never_resumes_unfinished_jobs(tmp_path):
    path=tmp_path/'jobs.sqlite'
    store=JobStore(path)
    identifier=store.create('campaign',[{'status':'sent','chat_id':1},{'status':'pending','chat_id':2}],'abc123')
    assert store.running()==identifier
    restarted=JobStore(path)
    job=restarted.get(identifier)
    assert job['state']=='interrupted' and [s['status'] for s in job['results']]==['sent','not_attempted'] and 'NOT run' in job['note'] and restarted.running() is None
    with pytest.raises(BackendError) as error:
        restarted.get('j_missing')
    assert error.value.code=='job_not_found'


# ---- leaving ----------------------------------------------------------------------------------------------------

def test_leave_previews_skips_owner_and_people_and_cleans_folders(world):
    engine,client=world
    async def scenario():
        draft=await engine.prepare_leave_chats([cid(client.barakholka),cid(client.chat),cid(client.mine),cid(client.friend),-1009999999999])
        rows={row['chat_id']:row for row in draft['preview']['chats']}
        assert rows[cid(client.barakholka)]['status']=='will_leave' and rows[cid(client.barakholka)]['in_folders']==['Реклама']
        assert rows[cid(client.mine)]['reason']=='you_are_the_owner' and rows[cid(client.friend)]['reason']=='only_groups_and_channels' and rows[-1009999999999]['reason']=='not_in_your_chat_list'
        assert draft['preview']['destructive'] and draft['preview']['will_leave']==2 and 'cache_note' in draft['preview'] and not sent_requests(client,f.channels.LeaveChannelRequest)
        assert engine.required_scope(draft['draft_id'])=='telegram.manage'
        return await do(engine,draft)
    result=go(scenario())
    assert result['job']['counts']=={'left':2} and sent_requests(client,f.channels.LeaveChannelRequest)[0].channel.channel_id==client.barakholka.id
    assert sent_requests(client,f.messages.DeleteChatUserRequest)[0].chat_id==client.chat.id and isinstance(sent_requests(client,f.messages.DeleteChatUserRequest)[0].user_id,t.InputUserSelf)
    history=sent_requests(client,f.messages.DeleteHistoryRequest)[0]
    assert history.max_id==0 and history.revoke is False and history.just_clear is False
    first=result['job']['steps'][0]
    assert first['status']=='left' and first['folder_cleanup']=='done' and first['removed_from_folders']==1
    assert client.filters[0].include_peers==[] and engine.limits_status()['used_last_24h']['leaves']==2


def test_leave_without_clearing_history_and_errors(world):
    engine,client=world
    async def scenario():
        keep=await engine.prepare_leave_chats([cid(client.chat)],clear_history=False)
        done=await do(engine,keep)
        assert done['job']['steps'][0]['history_deleted'] is False and not sent_requests(client,f.messages.DeleteHistoryRequest)
        client.leave_errors[client.news.id]=errors.UserNotParticipantError(request=None)
        gone=await do(engine,await engine.prepare_leave_chats([cid(client.news)]))
        assert gone['job']['steps'][0]['status']=='already_left'
        with pytest.raises(BackendError) as error:
            await engine.prepare_leave_chats([cid(client.mine),cid(client.friend)])
        assert error.value.code=='nothing_to_leave'
        with pytest.raises(BackendError):
            await engine.prepare_leave_chats([])
    go(scenario())


# ---- campaigns --------------------------------------------------------------------------------------------------

def test_campaign_preview_shows_every_chat_and_what_is_skipped(world):
    engine,client=world
    client.filters.append(folder(7,'Для рассылки',client.barakholka,client.barakholka2,client.news,client.muted,client.friend,client.free))
    client.dialogs.append(dialog(client.free))
    async def scenario():
        return await engine.prepare_campaign(text='Продаю диван, цена 9000',folder='Для рассылки')
    draft=go(scenario())
    preview=draft['preview']
    rows={row['chat_id']:row for row in preview['chats']}
    assert [rows[cid(e)]['status'] for e in (client.barakholka,client.barakholka2)]==['will_send','will_send']
    assert rows[cid(client.news)]['reason']=='channel_readonly' and rows[cid(client.muted)]['reason']=='members_cannot_post' and rows[cid(client.friend)]['reason']=='not_a_group'
    assert rows[cid(client.free)]['reason']=='not_a_member' and preview['will_send']==2 and preview['skipped']==4
    assert preview['text']=='Продаю диван, цена 9000' and preview['pause_between_seconds']==[60,180] and preview['estimated_minutes']==4 and 'warning' in preview and preview['safeguards']
    assert engine.required_scope(draft['draft_id'])=='telegram.send' and not sent_requests(client,f.messages.SendMessageRequest)


def test_campaign_sends_slowly_records_history_and_refuses_the_same_text_again(world):
    engine,client=world
    async def scenario():
        draft=await engine.prepare_campaign(text='Продаю диван',chat_ids=[cid(client.barakholka),cid(client.barakholka2)])
        result=await do(engine,draft)
        replay=await engine.execute_action(draft['draft_id'],draft['confirmation_hash'])
        again=None
        try:
            await engine.prepare_campaign(text='Продаю диван',chat_ids=[cid(client.barakholka)])
        except BackendError as error:
            again=error
        other=await engine.prepare_campaign(text='Другой текст',chat_ids=[cid(client.barakholka)])
        return draft,result,replay,again,other
    draft,result,replay,again,other=go(scenario())
    job=result['job']
    assert job['state']=='done' and job['counts']=={'sent':2} and [s['chat_id'] for s in job['steps']]==[cid(client.barakholka),cid(client.barakholka2)]
    sends=sent_requests(client,f.messages.SendMessageRequest)
    assert [r.message for r in sends]==['Продаю диван']*2 and len({r.random_id for r in sends})==2 and all(r.no_webpage for r in sends)
    assert replay['already_executed'] and len(sends)==2 and sent_requests(client,f.messages.SendMessageRequest)==sends
    assert again.code=='nothing_to_send' and again.diagnostics['chats'][0]['reason']=='same_text_sent_recently' and other['preview']['will_send']==1
    assert engine.limits_status()['used_last_24h']['campaign_messages']==2
    assert 'Продаю' not in json.dumps(job) and 'Продаю' not in json.dumps(engine.audit.entries())
    assert {e['action'] for e in engine.audit.entries()}>={'campaign_message','send_message'} or {'campaign_message'}<={e['action'] for e in engine.audit.entries()}


def test_campaign_limits_pauses_and_inputs(tmp_path):
    engine,client=build(tmp_path,TG_CAMPAIGN_MAX_CHATS=1,TG_CAMPAIGN_DAILY_LIMIT=1,TG_CAMPAIGN_MIN_DELAY=90)
    ids=[cid(client.barakholka),cid(client.barakholka2)]
    def expect(code,**kwargs):
        with pytest.raises(BackendError) as error:
            go(engine.prepare_campaign(**kwargs))
        assert error.value.code==code
        return error.value
    expect('too_many_chats_selected',text='x',chat_ids=ids)
    expect('invalid_input',text='x',chat_ids=ids[:1],pause_min_s=30)
    expect('invalid_input',text='x',chat_ids=ids[:1],pause_min_s=200,pause_max_s=100)
    expect('invalid_input',text='x')
    expect('invalid_input',text='x',chat_ids=ids[:1],folder='Реклама')
    expect('invalid_input',text='',chat_ids=ids[:1])
    assert go(engine.prepare_campaign(text='x',chat_ids=ids[:1]))['preview']['pause_between_seconds']==[90,210]
    go(do_campaign(engine,ids[:1],'первый'))
    assert expect('daily_limit_reached',text='второй',chat_ids=ids[1:]).diagnostics['limit_24h']==1


async def do_campaign(engine,ids,text):
    return await do(engine,await engine.prepare_campaign(text=text,chat_ids=ids))


def test_campaign_stops_on_flood_spam_or_unknown_outcome_and_skips_forbidden_chats(world):
    engine,client=world
    ids=[cid(client.barakholka),cid(client.barakholka2)]
    client.send_errors[cid(client.barakholka)]=errors.ChatWriteForbiddenError(request=None)
    job=go(do_campaign(engine,ids,'a'))['job']
    assert job['state']=='done' and [s['status'] for s in job['steps']]==['skipped','sent'] and job['steps'][0]['reason']=='no_permission'
    client.send_errors={cid(client.barakholka):errors.SlowModeWaitError(request=None,capture=40)}
    job=go(do_campaign(engine,ids,'b'))['job']
    assert job['steps'][0]['reason']=='slow_mode' and job['steps'][1]['status']=='sent'
    client.send_errors={cid(client.barakholka):errors.PeerFloodError(request=None)}
    job=go(do_campaign(engine,ids,'c'))['job']
    assert job['state']=='stopped' and job['steps'][0]['error']=='privacy_restricted' and job['steps'][1]['status']=='not_attempted'
    client.send_errors={cid(client.barakholka):errors.FloodWaitError(request=None,capture=900)}
    job=go(do_campaign(engine,ids,'d'))['job']
    assert job['state']=='stopped' and job['steps'][0]['error']=='rate_limited' and job['steps'][1]['status']=='not_attempted'
    client.send_errors={cid(client.barakholka):RuntimeError('unknown')}
    before=client.sent
    job=go(do_campaign(engine,ids,'e'))['job']
    assert job['state']=='stopped' and job['steps'][0]['error']=='telegram_error' and job['steps'][1]['status']=='not_attempted' and client.sent==before


def test_a_short_flood_wait_is_waited_out_once_and_the_same_random_id_is_reused(world):
    engine,client=world
    key=cid(client.barakholka)
    client.send_errors[key]=errors.FloodWaitError(request=None,capture=3)
    original=client.__call__.__func__

    async def once(self,request):
        if isinstance(request,f.messages.SendMessageRequest) and key in self.send_errors:
            self.requests.append(request)
            raise self.send_errors.pop(key)
        return await original(self,request)
    client.__class__=type('Flaky',(GroupClient,),{'__call__':once})
    job=go(do_campaign(engine,[key],'g'))['job']
    sends=sent_requests(client,f.messages.SendMessageRequest)
    assert job['state']=='done' and job['steps'][0]['status']=='sent' and len(sends)==2 and sends[0].random_id==sends[1].random_id


def test_campaign_with_a_photo_and_rights_are_rechecked_at_send_time(world):
    import base64,hashlib
    engine,client=world
    async def scenario():
        data=b'fabricated image'
        pending=await engine.files.begin('реклама.png','image/png',len(data),hashlib.sha256(data).hexdigest())
        asset=await engine.files.append(pending['upload_id'],0,base64.b64encode(data).decode(),True)
        draft=await engine.prepare_campaign(text='Подпись',chat_ids=[cid(client.barakholka)],file_id=asset['file_id'])
        assert draft['preview']['media'][0]['sha256']==asset['sha256']
        client.barakholka.default_banned_rights=t.ChatBannedRights(until_date=None,send_messages=True)
        client.dialogs[0]=dialog(client.barakholka)
        return await do(engine,draft)
    job=go(scenario())['job']
    assert job['steps'][0]=={**job['steps'][0],'status':'skipped','reason':'members_cannot_post'} and not sent_requests(client,f.messages.SendMediaRequest)
    async def with_media():
        client.barakholka.default_banned_rights=None
        client.dialogs[0]=dialog(client.barakholka)
        data=b'other image'
        pending=await engine.files.begin('b.png','image/png',len(data),hashlib.sha256(data).hexdigest())
        asset=await engine.files.append(pending['upload_id'],0,base64.b64encode(data).decode(),True)
        return await do(engine,await engine.prepare_campaign(text='Подпись2',chat_ids=[cid(client.barakholka)],file_id=asset['file_id']))
    assert go(with_media())['job']['steps'][0]['status']=='sent' and isinstance(sent_requests(client,f.messages.SendMediaRequest)[0].media,t.InputMediaUploadedPhoto)
    pdf=b'%PDF'
    async def bad_media():
        pending=await engine.files.begin('x.pdf','application/pdf',len(pdf))
        asset=await engine.files.append(pending['upload_id'],0,base64.b64encode(pdf).decode(),True)
        await engine.prepare_campaign(text='x',chat_ids=[cid(client.barakholka)],file_id=asset['file_id'])
    with pytest.raises(BackendError) as error:
        go(bad_media())
    assert error.value.code=='unsupported_media'


def test_limits_are_read_from_the_environment_and_clamped():
    assert Limits({}).as_dict()=={'joins_per_day':10,'leaves_per_day':30,'campaign_messages_per_day':20,'campaign_max_chats':30,'min_pause_seconds':45,'same_text_pause_days':14}
    custom=Limits({'TG_JOIN_DAILY_LIMIT':'3','TG_CAMPAIGN_DAILY_LIMIT':'junk','TG_CAMPAIGN_MAX_CHATS':'100000','TG_CAMPAIGN_MIN_DELAY':'-5'})
    assert custom.join_daily==3 and custom.message_daily==20 and custom.campaign_max_chats==200 and custom.min_delay==0


# ---- MCP surface ------------------------------------------------------------------------------------------------

@pytest.fixture
def web(tmp_path,oauth,monkeypatch):
    engine,client=build(tmp_path)
    owner,_,_=oauth
    monkeypatch.setenv('TG_DATA_DIR',str(tmp_path))
    with TestClient(create_app(engine,owner),base_url=owner.public_url) as app:
        yield app,owner,engine,client


def tool(app,owner,scopes,name,arguments):
    return rpc(app,bearer(owner,scopes),'tools/call',{'name':name,'arguments':arguments}).json()['result']['structuredContent']


def test_group_tools_are_listed_and_read_only_ones_are_marked(web):
    app,owner,_,_=web
    tools={x['name']:x for x in rpc(app,bearer(owner,['telegram.read']),'tools/list').json()['result']['tools']}
    assert len(tools)==108
    reads=['telegram_list_chats','telegram_search_public_chats','telegram_inspect_chat','telegram_list_folders','telegram_get_folder','telegram_job_status','telegram_list_jobs','telegram_limits_status']
    prepares=['telegram_prepare_join_chats','telegram_prepare_leave_chats','telegram_prepare_folder_change','telegram_prepare_campaign']
    assert all(tools[name]['annotations']['readOnlyHint'] is True for name in reads+prepares)
    assert tools['telegram_job_cancel']['annotations']['readOnlyHint'] is False
    assert 'untrusted' in tools['telegram_search_public_chats']['description'] and 'advertising' in tools['telegram_prepare_campaign']['description']


def test_join_through_mcp_returns_the_finished_job_and_needs_manage_scope(web):
    app,owner,engine,client=web
    send=['telegram.read','telegram.send']
    manage=send+['telegram.manage']
    draft=tool(app,owner,send,'telegram_prepare_join_chats',{'targets':['@ads_krym']})
    args={'draft_id':draft['draft_id'],'confirmation_hash':draft['confirmation_hash']}
    assert tool(app,owner,send,'telegram_execute_action',args)['error']=='insufficient_scope'
    assert tool(app,owner,send,'telegram_execute_management_action',args)['error']=='insufficient_scope' and not sent_requests(client,f.channels.JoinChannelRequest)
    done=tool(app,owner,manage,'telegram_execute_management_action',args)
    assert done['started'] and done['job']['state']=='done' and done['job']['counts']=={'joined':1}
    status=tool(app,['telegram.read'],'telegram_job_status',{'job_id':done['job_id']}) if False else tool(app,owner,['telegram.read'],'telegram_job_status',{'job_id':done['job_id']})
    assert status['state']=='done' and tool(app,owner,['telegram.read'],'telegram_job_status',{'job_id':'j_missing'})['error']=='job_not_found'
    assert tool(app,owner,['telegram.read'],'telegram_limits_status',{})['used_last_24h']['joins']==1
    assert tool(app,owner,['telegram.read'],'telegram_list_jobs',{})['jobs'][0]['job_id']==done['job_id']


def test_campaign_through_mcp_needs_send_scope_and_the_normal_tool(web):
    app,owner,engine,client=web
    read=['telegram.read']
    send=read+['telegram.send']
    manage=send+['telegram.manage']
    draft=tool(app,owner,read,'telegram_prepare_campaign',{'text':'Продаю диван','chat_ids':[cid(client.barakholka)]})
    args={'draft_id':draft['draft_id'],'confirmation_hash':draft['confirmation_hash']}
    assert tool(app,owner,read,'telegram_execute_action',args)['error']=='insufficient_scope'
    assert tool(app,owner,manage,'telegram_execute_management_action',args)['error']=='invalid_action' and not sent_requests(client,f.messages.SendMessageRequest)
    done=tool(app,owner,send,'telegram_execute_action',args)
    assert done['started'] and done['job']['counts']=={'sent':1} and len(sent_requests(client,f.messages.SendMessageRequest))==1


def test_folder_tools_through_mcp(web):
    app,owner,engine,client=web
    manage=['telegram.read','telegram.send','telegram.manage']
    assert tool(app,owner,manage,'telegram_list_folders',{})['folders'][0]['title']=='Реклама'
    assert tool(app,owner,manage,'telegram_list_chats',{'query':'барахолка','kind':'groups_and_channels'})['count']==3
    draft=tool(app,owner,manage,'telegram_prepare_folder_change',{'operation':'create','title':'Барахолки','chat_ids':[cid(client.barakholka2)]})
    assert tool(app,owner,manage,'telegram_execute_management_action',{'draft_id':draft['draft_id'],'confirmation_hash':draft['confirmation_hash']})['changed']
    assert tool(app,owner,manage,'telegram_get_folder',{'folder':'Барахолки'})['count']==1
    assert tool(app,owner,manage,'telegram_search_public_chats',{'query':'крым'})['count']==4
    assert tool(app,owner,manage,'telegram_inspect_chat',{'target':'@ads_krym'})['requires_approval'] is True
