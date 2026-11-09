"""TZ-bot-interaction 4.1-4.3 on a fabricated Telethon client: keyboards, presses, policy, audit."""
import asyncio
import json
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace as NS

import pytest
from starlette.testclient import TestClient
from telethon import errors
from telethon.tl import functions as f,types as t
from telethon.tl.types import messages as m

import bot_interaction
from bot_interaction import BotPolicy,scan
from extended_backend import ExtendedBackend
from telegram_backend import BackendError
from test_server import bearer,rpc
from test_oauth_owner import oauth
from server import create_app

NOW=datetime.now(timezone.utc)


def cb(text,data=b'x'):
    return t.KeyboardInlineButton(text=text,type=t.InlineButtonTypeCallback(data=data))


def link(text,kind):
    return t.KeyboardInlineButton(text=text,type=kind)


def inline(*rows):
    return t.ReplyInlineMarkup(rows=[t.KeyboardInlineButtonRow(buttons=list(row)) for row in rows])


def reply(*rows):
    return t.ReplyKeyboardMarkup(rows=[t.KeyboardButtonRow(buttons=list(row)) for row in rows],resize=True,persistent=True)


def plain(text,kind=None):
    return t.KeyboardButton(text=text,type=kind or t.ButtonTypeDefault())


class BotClient:
    def __init__(self):
        self.requests=[]
        self.callback_failure=None
        self.callback_failures=[]
        self.callback_delay=0
        self.on_callback=None
        self.owner=t.User(id=999,access_hash=1,first_name='Владелец',username='owner')
        self.bot=t.User(id=500,access_hash=3,first_name='Помощник',username='helper_bot',bot=True,bot_inline_placeholder='запрос')
        self.other_bot=t.User(id=600,access_hash=4,first_name='Другой',username='other_bot',bot=True)
        self.human=t.User(id=100,access_hash=2,first_name='Адресат')
        self.messages={500:{},600:{},100:{}}

    def add(self,chat,identifier,text='Выберите действие',markup=None,out=False,edit_date=None):
        self.messages[chat][identifier]=NS(id=identifier,chat_id=chat,date=NOW,message=text,file=None,media=None,out=out,
                                           sender_id=999 if out else chat,reply_markup=markup,edit_date=edit_date)
        return self.messages[chat][identifier]

    def is_connected(self): return True
    async def connect(self): pass
    async def disconnect(self): pass
    async def is_user_authorized(self): return True
    async def get_me(self): return self.owner
    async def get_entity(self,name): return self.bot
    async def get_input_entity(self,entity):
        if isinstance(entity,t.InputPeerSelf): return entity
        value=getattr(entity,'id',entity)
        return t.InputPeerSelf() if value in (999,'me') else t.InputPeerUser(value,2)
    async def iter_dialogs(self):
        for entity in (self.bot,self.other_bot,self.human):
            yield NS(id=entity.id,entity=entity,name=entity.first_name,is_channel=False,is_group=False)
    async def iter_messages(self,entity,**kwargs):
        values=sorted(self.messages.get(getattr(entity,'id',999),{}).values(),key=lambda x:-x.id)
        offset=kwargs.get('offset_id',0)
        for item in [x for x in values if not offset or x.id<offset][:kwargs['limit']]:
            yield item
    async def get_messages(self,entity,ids):
        store=self.messages.get(getattr(entity,'id',999),{})
        found=[store.get(i) for i in ids] if isinstance(ids,list) else store.get(ids)
        return found
    async def upload_file(self,path,file_name):
        return t.InputFile(id=200,parts=1,name=file_name,md5_checksum='')
    async def __call__(self,request):
        self.requests.append(request)
        bytes(request)
        if isinstance(request,f.messages.GetBotCallbackAnswerRequest):
            if self.callback_delay:
                await asyncio.sleep(self.callback_delay)
            if self.callback_failures:
                raise self.callback_failures.pop(0)
            if self.callback_failure:
                raise self.callback_failure
            if self.on_callback:
                self.on_callback()
            return m.BotCallbackAnswer(cache_time=0,message='Отменено',alert=False)
        if isinstance(request,f.users.GetFullUserRequest):
            info=t.BotInfo(description='Бот для тестов',commands=[t.BotCommand(command='start',description='Запуск'),t.BotCommand(command='status',description='Статус')],
                           menu_button=t.BotMenuButtonCommands())
            return NS(full_user=NS(bot_info=info))
        return t.Updates(updates=[t.UpdateMessageID(id=777,random_id=getattr(request,'random_id',0) or 0)],users=[],chats=[],date=NOW,seq=1)


@pytest.fixture
def world(tmp_path):
    client=BotClient()
    engine=ExtendedBackend(client=client,allow_send=True,output_dir=tmp_path/'output',drafts_db=tmp_path/'drafts.sqlite',bot_policy=BotPolicy(None))
    return engine,client


def run(operation):
    return asyncio.run(operation)


def press(engine,chat,message,**kw):
    return run(engine.prepare_press_button(chat,message,**kw))


def execute(engine,draft):
    return run(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))


def callbacks(client):
    return [r for r in client.requests if isinstance(r,f.messages.GetBotCallbackAnswerRequest)]


def test_every_button_type_is_recognized_and_callback_data_stays_private(world):
    engine,client=world
    kinds=[('callback',cb('Отмена',b'secret-payload')),('url',link('Сайт',t.InlineButtonTypeUrl(url='https://example.com'))),
           ('login_url',link('Вход',t.InlineButtonTypeUrlAuth(url='https://site.example/login',button_id=1))),
           ('web_app',link('Апп',t.InlineButtonTypeWebView(url='https://app.example'))),
           ('switch_inline',link('Поиск',t.InlineButtonTypeSwitchInline(query='q'))),
           ('switch_inline_current',link('Тут',t.InlineButtonTypeSwitchInline(query='q',same_peer=True))),
           ('copy_text',link('Код',t.InlineButtonTypeCopy(copy_text='ABC-1'))),
           ('user_profile',link('Профиль',t.InlineButtonTypeUserProfile(user_id=7))),
           ('game',link('Игра',t.InlineButtonTypeGame())),('buy',link('Купить',t.InlineButtonTypeBuy()))]
    client.add(500,10,markup=inline(*[[button] for _,button in kinds]))
    result=run(engine.get_keyboard(500,10))
    assert result['kind']=='inline' and [b['type'] for b in result['buttons']]==[kind for kind,_ in kinds]
    assert [b['has_data'] for b in result['buttons']]==[True]+[False]*9
    assert result['buttons'][1]['url']=='https://example.com' and result['buttons'][6]['copy_text']=='ABC-1'
    dumped=json.dumps(result,ensure_ascii=False)
    assert 'secret-payload' not in dumped and '736563726574' not in dumped and '"data"' not in dumped
    assert 'untrusted' in result['data_trust']
    client.add(500,11,markup=reply([plain('📋 Статус'),plain('Номер',t.ButtonTypeRequestPhone())],[plain('Где я',t.ButtonTypeRequestGeoLocation()),plain('Опрос',t.ButtonTypeRequestPoll())]))
    assert [b['type'] for b in run(engine.get_keyboard(500,11))['buttons']]==['text','request_phone','request_location','request_poll']


def test_legacy_layer_classes_are_recognized_by_name():
    Callback=type('KeyboardButtonCallback',(),{})
    Url=type('KeyboardButtonUrl',(),{})
    Plain=type('KeyboardButton',(),{})
    Row=type('KeyboardButtonRow',(),{})
    Markup=type('ReplyInlineMarkup',(),{})
    callback,url,text=Callback(),Url(),Plain()
    callback.text,callback.data,callback.requires_password='Да',b'1',False
    url.text,url.url='Ссылка','https://example.com'
    text.text='Просто'
    row=Row()
    row.buttons=[callback,url,text]
    markup=Markup()
    markup.rows=[row]
    kind,items=scan(5,NS(id=3,reply_markup=markup))
    assert kind=='inline' and [i['type'] for i in items]==['callback','url','text'] and items[1]['url']=='https://example.com'


def test_button_ref_is_stable_and_changes_with_anything_pressable(world):
    engine,client=world
    client.add(500,10,markup=inline([cb('Да',b'1'),cb('Нет',b'2')]))
    first=run(engine.get_keyboard(500,10))['buttons']
    assert first==run(engine.get_keyboard(500,10))['buttons']
    assert first[0]['button_ref']!=first[1]['button_ref'] and first[0]['button_ref'].startswith('b_')
    client.messages[500][10].reply_markup=inline([cb('Да',b'CHANGED'),cb('Нет',b'2')])
    changed=run(engine.get_keyboard(500,10))['buttons']
    assert changed[0]['button_ref']!=first[0]['button_ref'] and changed[1]['button_ref']==first[1]['button_ref']
    client.messages[500][10].reply_markup=inline([cb('Да!',b'1'),cb('Нет',b'2')])
    assert run(engine.get_keyboard(500,10))['buttons'][0]['button_ref']!=first[0]['button_ref']


def test_message_listing_carries_keyboard_and_edit_time(world):
    engine,client=world
    client.add(500,10,markup=inline([cb('Да')]),edit_date=NOW)
    client.add(500,9,text='Без клавиатуры')
    messages=run(engine.read_history(500,limit=10))['messages']
    assert messages[0]['keyboard']['kind']=='inline' and messages[0]['edited_at']==NOW.isoformat()
    assert 'keyboard' not in messages[1] and 'edited_at' not in messages[1]


def test_press_is_reviewed_single_use_and_returns_callback_answer(world):
    engine,client=world
    client.add(500,10,text='Поставить все кондиционеры?',markup=inline([cb('✅ Да',b'yes'),cb('Отмена',b'cancel')]))
    draft=press(engine,500,10,text='Отмена')
    assert not client.requests and draft['preview']['button']['text']=='Отмена'
    assert draft['preview']['source_message']['text']=='Поставить все кондиционеры?' and draft['preview']['chat']['chat_id']==500
    assert 'cancel' not in json.dumps(draft) and engine.required_scope(draft['draft_id'])=='telegram.send'
    result=execute(engine,draft)
    assert result['callback_answer']=={'text':'Отменено','alert':False,'url':None} and result['callback_timeout'] is False
    assert callbacks(client)[0].data==b'cancel' and callbacks(client)[0].msg_id==10
    assert execute(engine,draft)['already_executed'] and len(callbacks(client))==1


def test_callback_without_bot_answer_is_not_an_error_and_never_retried(world,monkeypatch):
    engine,client=world
    client.add(500,10,markup=inline([cb('Запуск')]))
    client.callback_failure=errors.BotResponseTimeoutError(request=None)
    result=execute(engine,press(engine,500,10,text='Запуск'))
    assert result['executed'] and result['callback_answer'] is None and result['callback_timeout'] is True and 'Do NOT press again' in result['note']
    assert len(callbacks(client))==1
    client.callback_failure=None
    monkeypatch.setattr(bot_interaction,'CALLBACK_WAIT',0.05)
    client.callback_delay=1
    slow=execute(engine,press(engine,500,10,text='Запуск'))
    assert slow['callback_answer'] is None and slow['callback_timeout'] is True and len(callbacks(client))==2


def test_stale_keyboard_is_refused_at_prepare_and_before_execution(world):
    engine,client=world
    client.add(500,10,markup=inline([cb('Да',b'1'),cb('Нет',b'2')]))
    old=run(engine.get_keyboard(500,10))['buttons'][0]['button_ref']
    draft=press(engine,500,10,button_ref=old)
    client.messages[500][10].reply_markup=inline([cb('Другая',b'3')])
    with pytest.raises(BackendError) as error:
        execute(engine,draft)
    assert error.value.code=='stale_button' and [b['text'] for b in error.value.diagnostics['keyboard']]==['Другая']
    assert not callbacks(client)
    with pytest.raises(BackendError) as error:
        press(engine,500,10,button_ref=old)
    assert error.value.code=='stale_button' and error.value.diagnostics['keyboard'][0]['text']=='Другая'
    fresh=press(engine,500,10,button_ref=error.value.diagnostics['keyboard'][0]['button_ref'])
    assert execute(engine,fresh)['executed'] and len(callbacks(client))==1


def test_button_selection_errors_list_the_choices(world):
    engine,client=world
    client.add(500,10,markup=inline([cb('Да',b'1'),cb('Да',b'2')],[cb('Нет',b'3')]))
    with pytest.raises(BackendError) as error:
        press(engine,500,10,text='Да')
    assert error.value.code=='ambiguous_button' and len(error.value.diagnostics['candidates'])==2
    with pytest.raises(BackendError) as error:
        press(engine,500,10,text='Может')
    assert error.value.code=='button_not_found' and len(error.value.diagnostics['keyboard'])==3
    with pytest.raises(BackendError) as error:
        press(engine,500,10)
    assert error.value.code=='invalid_input'
    client.add(500,11,text='Без клавиатуры')
    with pytest.raises(BackendError) as error:
        press(engine,500,11,text='Да')
    assert error.value.code=='button_not_found'
    with pytest.raises(BackendError) as error:
        press(engine,500,99,text='Да')
    assert error.value.code=='message_not_found'


@pytest.mark.parametrize('label,button',[('request_phone',plain('Номер',t.ButtonTypeRequestPhone())),('request_location',plain('Гео',t.ButtonTypeRequestGeoLocation())),
    ('request_poll',plain('Опрос',t.ButtonTypeRequestPoll())),('buy',link('Купить',t.InlineButtonTypeBuy())),('game',link('Играть',t.InlineButtonTypeGame())),
    ('login_url',link('Вход',t.InlineButtonTypeUrlAuth(url='https://site.example/login',button_id=1)))])
def test_forbidden_buttons_can_never_be_pressed(world,label,button):
    engine,client=world
    client.add(500,10,markup=reply([button]) if label.startswith('request') else inline([button]))
    with pytest.raises(BackendError) as error:
        press(engine,500,10,text=button.text)
    assert error.value.code=='forbidden_button' and error.value.diagnostics['button']['type']==label
    assert not client.requests
    engine.bot_policy=BotPolicy({'bots':{'helper_bot':{'actions':['press_button']}}})
    with pytest.raises(BackendError) as error:
        press(engine,500,10,text=button.text,auto_confirm=True)
    assert error.value.code=='forbidden_button' and not client.requests
    if label=='login_url':
        assert error.value.diagnostics['url']=='https://site.example/login'


def test_link_buttons_are_returned_as_data_and_never_opened(world):
    engine,client=world
    client.add(500,10,markup=inline([link('Сайт',t.InlineButtonTypeUrl(url='https://example.com/a')),link('Апп',t.InlineButtonTypeWebView(url='https://app.example/x')),
                                     link('Код',t.InlineButtonTypeCopy(copy_text='PROMO'))]))
    site=press(engine,500,10,text='Сайт')
    assert site['executed'] is False and site['url']=='https://example.com/a' and 'draft_id' not in site
    assert press(engine,500,10,text='Апп')['url']=='https://app.example/x'
    assert press(engine,500,10,text='Код')['copy_text']=='PROMO'
    client.add(500,11,markup=reply([plain('📋 Статус')]))
    text=press(engine,500,11,text='📋 Статус')
    assert text['type']=='text' and text['text']=='📋 Статус' and 'telegram_prepare_send' in text['instruction']
    assert not client.requests


def test_flood_wait_is_reported_without_waiting_and_draft_stays_retryable(world,monkeypatch):
    engine,client=world
    client.add(500,10,markup=inline([cb('Да')]))
    draft=press(engine,500,10,text='Да')
    client.callback_failure=errors.FloodWaitError(request=None,capture=60)
    with pytest.raises(BackendError) as error:
        execute(engine,draft)
    assert error.value.code=='flood_wait' and error.value.retry_after_seconds==60 and len(callbacks(client))==1
    client.callback_failure=None
    assert execute(engine,draft)['executed']
    slept=[]

    async def fake_sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(bot_interaction.asyncio,'sleep',fake_sleep)
    short=press(engine,500,10,text='Да')
    client.callback_failures=[errors.FloodWaitError(request=None,capture=3)]
    assert execute(engine,short)['executed'] and slept==[4] and len(callbacks(client))==4


def test_unknown_failure_during_press_is_uncertain_and_blocks_a_second_press(world):
    engine,client=world
    client.add(500,10,markup=inline([cb('Да')]))
    draft=press(engine,500,10,text='Да')
    client.callback_failure=RuntimeError('boom')
    with pytest.raises(BackendError) as error:
        execute(engine,draft)
    assert error.value.code=='telegram_error'
    client.callback_failure=None
    with pytest.raises(BackendError) as error:
        execute(engine,draft)
    assert error.value.code=='delivery_uncertain' and len(callbacks(client))==1


def test_auto_confirm_is_limited_to_the_owner_allow_list(world):
    engine,client=world
    client.add(500,10,markup=inline([cb('Да')]))
    client.add(600,10,markup=inline([cb('Да')]))
    with pytest.raises(BackendError) as error:
        press(engine,500,10,text='Да',auto_confirm=True)
    assert error.value.code=='bot_not_allowlisted'
    engine.bot_policy=BotPolicy({'bots':{'helper_bot':{'actions':['press_button']},'voice_only_bot':{'actions':['send_voice']}}})
    assert press(engine,500,10,text='Да',auto_confirm=True)['auto_confirm_authorized'] is True
    for chat in (600,):
        with pytest.raises(BackendError) as error:
            press(engine,chat,10,text='Да',auto_confirm=True)
        assert error.value.code=='bot_not_allowlisted'
    assert 'auto_confirm_authorized' not in press(engine,500,10,text='Да')
    engine.bot_policy=BotPolicy({'bots':{'helper_bot':{'actions':['send_message']}}})
    with pytest.raises(BackendError) as error:
        press(engine,500,10,text='Да',auto_confirm=True)
    assert error.value.code=='bot_not_allowlisted'
    engine.bot_policy=BotPolicy({'bots':{'anyone':{'actions':['press_button'],'chat_ids':[600]}}})
    assert press(engine,600,10,text='Да',auto_confirm=True)['auto_confirm_authorized']


def test_policy_loads_only_from_the_server_environment_and_fails_closed(tmp_path,monkeypatch):
    path=tmp_path/'fastpath.json'
    path.write_text(json.dumps({'bots':{'@Flycited_Bot':{'actions':['press_button','delete_everything']}}}))
    monkeypatch.setenv('TG_BOT_FASTPATH_FILE',str(path))
    policy=BotPolicy.from_env()
    assert policy.allows('press_button',{'flycited_bot'},1) and not policy.allows('delete_everything',{'flycited_bot'},1)
    path.write_text('not json')
    assert not BotPolicy.from_env().allows('press_button',{'flycited_bot'},1)
    monkeypatch.delenv('TG_BOT_FASTPATH_FILE')
    monkeypatch.setenv('TG_BOT_FASTPATH','{"bots":{"a_bot":{"actions":["send_voice"]}}}')
    assert BotPolicy.from_env().allows('send_voice',{'a_bot'},1)


def test_wait_for_bot_reply_returns_new_and_edited_messages(world,monkeypatch):
    engine,client=world
    monkeypatch.setattr(bot_interaction,'POLL_SECONDS',0.01)
    monkeypatch.setattr(bot_interaction,'SETTLE_SECONDS',0.01)
    client.add(500,10,text='Меню',markup=inline([cb('Да')]))
    quiet=run(engine.wait_for_bot_reply(500,10,timeout_s=1))
    assert quiet['timeout'] is True and quiet['messages']==[]
    client.add(500,11,text='Ответ бота',markup=inline([cb('Дальше')]))
    client.add(500,12,text='Моё сообщение',out=True)
    found=run(engine.wait_for_bot_reply(500,10,timeout_s=1))
    assert [x['message_id'] for x in found['messages']]==[11] and found['messages'][0]['keyboard']['buttons'][0]['text']=='Дальше'
    since=(datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()
    client.messages[500][10].edit_date=datetime.now(timezone.utc)
    edited=run(engine.wait_for_bot_reply(500,12,timeout_s=1,edited_since=since))
    assert [(x['message_id'],x['edited']) for x in edited['messages']]==[(10,True)]
    with pytest.raises(BackendError):
        run(engine.wait_for_bot_reply(500,10,timeout_s=500))


def test_bot_info_and_reply_keyboard(world):
    engine,client=world
    info=run(engine.get_bot_info('@helper_bot'))
    assert info['username']=='helper_bot' and [c['command'] for c in info['commands']]==['start','status']
    assert info['menu_button']['type']=='commands' and info['supports_inline'] is True and info['description']=='Бот для тестов'
    assert run(engine.get_bot_info(500))['username']=='helper_bot'
    with pytest.raises(BackendError) as error:
        run(engine.get_bot_info(100))
    assert error.value.code=='not_a_bot'
    assert run(engine.get_reply_keyboard(500))['kind']=='none'
    client.add(500,10,text='Меню',markup=reply([plain('📋 Статус'),plain('Помощь')],[plain('Номер',t.ButtonTypeRequestPhone())]))
    keyboard=run(engine.get_reply_keyboard(500))
    assert keyboard['kind']=='reply' and keyboard['rows']==[['📋 Статус','Помощь'],['Номер']] and keyboard['persistent'] and keyboard['resize']
    client.add(500,11,text='Скрыто',markup=t.ReplyKeyboardHide())
    assert run(engine.get_reply_keyboard(500))['kind']=='none'


def test_command_and_reply_are_marked_in_the_preview(world):
    engine,client=world
    client.add(500,5,text='Нужна цена')
    draft=run(engine.prepare_send(500,'/make 10 холодильники beko=3',options={'reply_to':5}))
    preview=draft['preview']
    assert preview['bot_command']=={'command':'/make','bot':None,'arguments':'10 холодильники beko=3'} and preview['recipient_is_bot'] is True
    assert preview['reply_to_message']['text']=='Нужна цена'
    plain_text=run(engine.prepare_send(500,'всё обычно'))['preview']
    assert 'bot_command' not in plain_text and 'reply_to_message' not in plain_text
    assert 'recipient_is_bot' not in run(engine.prepare_send(100,'/start'))['preview']


def test_forward_can_drop_the_author(world):
    engine,client=world
    client.add(100,4,text='Прайс')
    draft=run(engine.prepare_message_action('forward',100,[4],target_chat_id=500,drop_author=True))
    assert draft['preview']['drop_author'] is True
    execute(engine,draft)
    sent=client.requests[-1]
    assert isinstance(sent,f.messages.ForwardMessagesRequest) and sent.drop_author is True
    keep=run(engine.prepare_message_action('forward',100,[4],target_chat_id=500))
    execute(engine,keep)
    assert not client.requests[-1].drop_author


def test_audit_records_who_what_where_but_never_text_or_data(world):
    engine,client=world
    client.add(500,10,text='Секретный текст сообщения',markup=inline([cb('Да',b'private-data')]))
    execute(engine,press(engine,500,10,text='Да'))
    with pytest.raises(BackendError):
        press(engine,500,10,text='Нет такой')
    entries=engine.audit.entries()
    assert [(e['action'],e['status'],e['chat_id'],e['message_id'],e['client']) for e in entries]==[('press_button','executed',500,10,'local'),('prepare_press_button','prepared',500,10,'local')]
    dumped=json.dumps(entries,ensure_ascii=False)
    assert 'Секретный' not in dumped and 'private-data' not in dumped and 'Да' not in dumped


@pytest.fixture
def web(world,oauth,tmp_path,monkeypatch):
    engine,client=world
    owner,_,_=oauth
    monkeypatch.setenv('TG_DATA_DIR',str(tmp_path))
    monkeypatch.setattr(bot_interaction,'POLL_SECONDS',0.01)
    monkeypatch.setattr(bot_interaction,'SETTLE_SECONDS',0.01)
    with TestClient(create_app(engine,owner),base_url=owner.public_url) as app:
        yield app,owner,engine,client


def tool(app,owner,scopes,name,arguments):
    return rpc(app,bearer(owner,scopes),'tools/call',{'name':name,'arguments':arguments}).json()['result']['structuredContent']


def test_new_tools_are_listed_with_untrusted_data_warnings(web):
    app,owner,_,_=web
    tools={x['name']:x for x in rpc(app,bearer(owner,['telegram.read']),'tools/list').json()['result']['tools']}
    for name in ('telegram_get_keyboard','telegram_get_reply_keyboard','telegram_get_bot_info','telegram_wait_for_bot_reply','telegram_prepare_press_button','telegram_prepare_voice'):
        assert name in tools
    assert len(tools)==110
    assert tools['telegram_get_keyboard']['annotations']['readOnlyHint'] is True
    assert tools['telegram_prepare_press_button']['annotations']['readOnlyHint'] is False
    assert 'untrusted' in tools['telegram_get_keyboard']['description'] and 'untrusted' in tools['telegram_wait_for_bot_reply']['description']
    assert 'wait_reply_s' in tools['telegram_execute_action']['inputSchema']['properties']


def test_press_then_execute_with_wait_returns_the_bot_follow_up(web):
    app,owner,engine,client=web
    client.add(500,10,text='Поставить все кондиционеры?',markup=inline([cb('Отмена',b'cancel')]))
    client.on_callback=lambda:client.add(500,11,text='Хорошо, не ставлю')
    scopes=['telegram.read','telegram.send']
    draft=tool(app,owner,scopes,'telegram_prepare_press_button',{'chat_id':500,'message_id':10,'text':'Отмена'})
    done=tool(app,owner,scopes,'telegram_execute_action',{'draft_id':draft['draft_id'],'confirmation_hash':draft['confirmation_hash'],'wait_reply_s':5})
    assert done['callback_answer']['text']=='Отменено' and [x['text'] for x in done['bot_messages']]==['Хорошо, не ставлю']
    assert done['bot_reply_timeout'] is False and 'baseline_message_id' in done
    replay=tool(app,owner,scopes,'telegram_execute_action',{'draft_id':draft['draft_id'],'confirmation_hash':draft['confirmation_hash'],'wait_reply_s':5})
    assert replay['already_executed'] and len(callbacks(client))==1


def test_one_call_press_requires_allow_list_and_send_scope(web):
    app,owner,engine,client=web
    client.add(500,10,markup=inline([cb('Да',b'yes')]))
    args={'chat_id':500,'message_id':10,'text':'Да','auto_confirm':True}
    assert tool(app,owner,['telegram.read','telegram.send'],'telegram_prepare_press_button',args)['error']=='bot_not_allowlisted'
    engine.bot_policy=BotPolicy({'bots':{'helper_bot':{'actions':['press_button']}}})
    assert tool(app,owner,['telegram.read'],'telegram_prepare_press_button',args)['error']=='insufficient_scope' and not callbacks(client)
    done=tool(app,owner,['telegram.read','telegram.send'],'telegram_prepare_press_button',args)
    assert done['auto_confirmed'] is True and done['callback_answer']['text']=='Отменено' and len(callbacks(client))==1
    assert done['preview']['button']['text']=='Да'
    engine.bot_policy=BotPolicy({'bots':{'helper_bot':{'actions':['press_button']}}})
    client.messages[500][10].reply_markup=inline([link('Купить',t.InlineButtonTypeBuy())])
    assert tool(app,owner,['telegram.read','telegram.send'],'telegram_prepare_press_button',{**args,'text':'Купить'})['error']=='forbidden_button'


def test_voice_message_needs_ogg_and_obeys_the_allow_list(web):
    import asyncio as aio
    import base64
    import hashlib
    app,owner,engine,client=web

    def stage(data,name,mime):
        async def operation():
            pending=await engine.files.begin(name,mime,len(data),hashlib.sha256(data).hexdigest())
            return await engine.files.append(pending['upload_id'],0,base64.b64encode(data).decode(),True)
        return aio.run(operation())
    ogg=stage(b'OggS fabricated opus bytes','голос.ogg','audio/ogg')
    mp3=stage(b'ID3 fabricated','голос.mp3','audio/mpeg')
    client.add(500,5,text='Запишите голосовое')
    scopes=['telegram.read','telegram.send']
    draft=tool(app,owner,scopes,'telegram_prepare_voice',{'chat_id':500,'file_id':ogg['file_id'],'reply_to':5})
    assert draft['preview']['media'][0]['kind']=='voice' and draft['preview']['options']['reply_to']==5 and draft['preview']['recipient_is_bot']
    assert tool(app,owner,scopes,'telegram_prepare_voice',{'chat_id':500,'file_id':mp3['file_id']})['error']=='unsupported_media'
    assert tool(app,owner,scopes,'telegram_prepare_voice',{'chat_id':500,'file_id':ogg['file_id'],'auto_confirm':True})['error']=='bot_not_allowlisted'
    assert not client.requests
