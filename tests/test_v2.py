"""V2 acceptance contracts on fabricated data: exact bytes, real TL schemas, no sends."""
import asyncio
import base64
import hashlib
import io
import json
import socket
import zipfile
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PIL import Image
from starlette.testclient import TestClient
from telethon.tl import functions as f,types as t

from extended_backend import ExtendedBackend
from file_store import FileStore
from telegram_backend import BackendError
from media_processing import read_document
from test_server import bearer,rpc
from test_oauth_owner import oauth
from server import create_app


class Client:
    def __init__(self):
        self.connected=True
        self.requests=[]
        self.failure=None
        self.dialog_reads=0
        self.owner=t.User(id=999,access_hash=1,first_name='Владелец',username='owner')
        self.other=t.User(id=100,access_hash=2,first_name='Адресат')
        self.history=[NS(id=i,chat_id=999,date=None,message=f'Сообщение {i}',file=None,media=None,out=True,sender_id=999) for i in (9,8,7)]

    def is_connected(self): return self.connected
    async def connect(self): self.connected=True
    async def disconnect(self): self.connected=False
    async def is_user_authorized(self): return True
    async def get_me(self): return self.owner
    async def get_input_entity(self,entity):
        if isinstance(entity,t.InputPeerSelf): return entity
        value=getattr(entity,'id',entity)
        return t.InputPeerSelf() if value==999 or value=='me' else t.InputPeerUser(value,2)
    async def iter_dialogs(self):
        self.dialog_reads+=1
        yield NS(id=100,entity=self.other,name='Адресат',is_channel=False,is_group=False)
    async def iter_messages(self,entity,**kwargs):
        selected=999 if isinstance(entity,t.InputPeerSelf) else entity.id
        values=self.history if selected==999 else [NS(id=4,chat_id=100,date=None,message='Исходное',file=None,media=None,out=True,sender_id=100)]
        offset=kwargs.get('offset_id',0)
        for item in [m for m in values if not offset or m.id<offset][:kwargs['limit']]:
            yield item
    async def get_messages(self,entity,ids):
        selected=999 if isinstance(entity,t.InputPeerSelf) else entity.id
        def get(identifier):
            return NS(id=identifier,chat_id=selected,date=None,message='Исходное',file=None,media=None,out=True,sender_id=selected)
        return [get(i) for i in ids] if isinstance(ids,list) else get(ids)
    async def upload_file(self,path,file_name):
        assert Path(path).is_file()
        return t.InputFile(id=200,parts=1,name=file_name,md5_checksum='')
    async def __call__(self,request):
        self.requests.append(request)
        # Serialize against the ACTUAL Telethon TL schema, catching constructor/field bugs.
        bytes(request)
        if self.failure:
            raise self.failure
        if isinstance(request,f.messages.UploadMediaRequest):
            return t.MessageMediaPhoto(photo=t.Photo(id=500,access_hash=10,file_reference=b'fake',date=datetime.now(timezone.utc),sizes=[],dc_id=2))
        return t.Updates(updates=[t.UpdateMessageID(id=123,random_id=getattr(request,'random_id',0) or 0)],users=[],chats=[],date=datetime.now(timezone.utc),seq=1)


@pytest.fixture
def backend(tmp_path):
    client=Client()
    instance=ExtendedBackend(client=client,allow_send=True,output_dir=tmp_path/'output',drafts_db=tmp_path/'drafts.sqlite')
    return instance,client


def stage(backend,data=b'Original bytes',filename='исходный.txt',mime='text/plain'):
    async def operation():
        pending=await backend.files.begin(filename,mime,len(data),hashlib.sha256(data).hexdigest())
        return await backend.files.append(pending['upload_id'],0,base64.b64encode(data).decode(),True)
    return asyncio.run(operation())


def test_saved_uses_self_without_dialog_name_search_and_paginates(backend):
    engine,client=backend
    first=asyncio.run(engine.get_saved_messages(limit=2))
    assert first['chat']['chat_id']==999 and first['next_offset_id']==8
    second=asyncio.run(engine.get_saved_messages(limit=2,offset_id=8))
    assert [m['message_id'] for m in second['messages']]==[7]
    assert client.dialog_reads==0


def test_saved_text_preview_unicode_and_single_use_send(backend):
    engine,client=backend
    draft=asyncio.run(engine.prepare_send(None,'Привет 👋'))
    assert draft['preview']['recipient']['chat_id']==999 and not client.requests
    result=asyncio.run(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))
    assert result['message_ids']==[123]
    assert client.requests[0].message=='Привет 👋'
    assert isinstance(client.requests[0].peer,t.InputPeerSelf)
    assert asyncio.run(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))['already_executed']
    assert len(client.requests)==1


def test_confirmation_mismatch_and_timeout_never_duplicate(backend):
    engine,client=backend
    draft=asyncio.run(engine.prepare_send(None,'Точное содержимое'))
    with pytest.raises(BackendError,match='confirmation hash'):
        asyncio.run(engine.execute_action(draft['draft_id'],'0'*64))
    assert not client.requests
    client.failure=TimeoutError()
    with pytest.raises(BackendError) as error:
        asyncio.run(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))
    assert error.value.code=='timeout'
    with pytest.raises(BackendError) as error:
        asyncio.run(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))
    assert error.value.code=='delivery_uncertain' and len(client.requests)==1


def test_original_file_to_saved_preserves_name_hash_and_document_type(backend):
    engine,client=backend
    asset=stage(engine,b'\x00\x01original\xff','Отчёт.docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    draft=asyncio.run(engine.prepare_send(None,media=[{'file_id':asset['file_id']}]))
    assert draft['preview']['media'][0]['sha256']==asset['sha256']
    asyncio.run(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))
    sent=client.requests[-1]
    assert isinstance(sent,f.messages.SendMediaRequest)
    assert isinstance(sent.media,t.InputMediaUploadedDocument)
    assert sent.media.attributes[0].file_name=='Отчёт.docx'


def test_changed_bytes_fail_before_upload_or_send(backend):
    engine,client=backend
    asset=stage(engine)
    draft=asyncio.run(engine.prepare_send(None,media=[{'file_id':asset['file_id']}]))
    engine.files.path(asset['file_id']).write_bytes(b'TAMPERED')
    with pytest.raises(BackendError) as error:
        asyncio.run(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))
    assert error.value.code=='file_integrity_error' and not client.requests


def test_native_article_is_one_rich_message_and_order_is_exact(backend):
    engine,client=backend
    image=io.BytesIO()
    Image.new('RGB',(2,2),'red').save(image,format='PNG')
    asset=stage(engine,image.getvalue(),'карточка.png','image/png')
    draft=asyncio.run(engine.create_article('БытТехОпт',[{'type':'paragraph','text':'До фото'},{'type':'photo','file_id':asset['file_id']},{'type':'paragraph','text':'После фото'}]))
    assert draft['preview']['single_message']
    asyncio.run(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))
    sends=[r for r in client.requests if isinstance(r,f.messages.SendMessageRequest)]
    assert len(sends)==1
    assert [type(b).__name__ for b in sends[0].rich_message.blocks]==['PageBlockHeading1','PageBlockParagraph','PageBlockPhoto','PageBlockParagraph']
    assert sends[0].rich_message.photos[0].id==500
    assert isinstance(sends[0].rich_message.blocks[2].caption.text,t.TextEmpty)


def test_rich_article_preserves_three_heading_levels(backend):
    engine,client=backend
    draft=asyncio.run(engine.create_article('Заголовок',[{'type':'heading','text':f'Уровень {level}','level':level} for level in (1,2,3)]))
    asyncio.run(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))
    blocks=client.requests[-1].rich_message.blocks
    assert [type(block).__name__ for block in blocks]==['PageBlockHeading1','PageBlockHeading1','PageBlockHeading2','PageBlockHeading3']


@pytest.mark.parametrize('action', ['reply','forward','edit','react'])
def test_real_tl_message_action_contracts(backend,action):
    engine,client=backend
    draft=asyncio.run(engine.prepare_message_action(action,100,[4],text='Новый текст',emoji='👍',target_chat_id=None))
    result=asyncio.run(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))
    assert result['executed']
    assert isinstance(client.requests[-1],{'reply':f.messages.SendMessageRequest,'forward':f.messages.ForwardMessagesRequest,'edit':f.messages.EditMessageRequest,'react':f.messages.SendReactionRequest}[action])
    if action=='forward':
        assert isinstance(client.requests[-1].to_peer,t.InputPeerSelf)


def test_story_privacy_period_and_result_use_story_api(backend):
    engine,client=backend
    asset=stage(engine,b'fabricated-photo-bytes','story.jpg','image/jpeg')
    draft=asyncio.run(engine.prepare_story(asset['file_id'],'Подпись'))
    assert draft['preview']['options']['audience']=='contacts'
    assert draft['preview']['recipient']['type']=='personal_profile'
    assert draft['preview']['recipient']['title'].startswith('Профиль')
    asyncio.run(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))
    request=client.requests[-1]
    assert isinstance(request,f.stories.SendStoryRequest)
    assert isinstance(request.privacy_rules[0],t.InputPrivacyValueAllowContacts)
    assert request.period==86400


def test_story_clickable_link_uses_native_media_area(backend):
    engine,client=backend
    asset=stage(engine,b'photo','story.jpg','image/jpeg')
    draft=asyncio.run(engine.prepare_story(asset['file_id'],options={'links':[{'url':'https://example.com/info'}]}))
    asyncio.run(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))
    assert isinstance(client.requests[-1].media_areas[0],t.MediaAreaUrl)
    assert client.requests[-1].media_areas[0].url=='https://example.com/info'


def test_story_cannot_target_another_person_and_schedule_requires_timezone(backend):
    engine,client=backend
    asset=stage(engine,b'bytes','story.jpg','image/jpeg')
    with pytest.raises(BackendError) as error:
        asyncio.run(engine.prepare_story(asset['file_id'],chat_id=100))
    assert error.value.code=='permission_denied'
    with pytest.raises(BackendError):
        asyncio.run(engine.prepare_send(None,'schedule',options={'schedule_at':'2099-01-01T12:00:00'}))
    assert not client.requests


def test_upload_bounds_offsets_hash_and_private_path(tmp_path):
    files=FileStore(tmp_path,max_bytes=10,quota=12)
    pending=asyncio.run(files.begin('../../личный.txt','text/plain',5,hashlib.sha256(b'hello').hexdigest()))
    assert '..' not in files.path(pending['upload_id']).name
    asyncio.run(files.append(pending['upload_id'],0,base64.b64encode(b'he').decode()))
    with pytest.raises(BackendError):
        asyncio.run(files.append(pending['upload_id'],0,'aGU='))
    info=asyncio.run(files.append(pending['upload_id'],2,base64.b64encode(b'llo').decode(),True))
    assert files.verified_path(info['file_id']).read_bytes()==b'hello'
    with pytest.raises(BackendError):
        asyncio.run(files.begin('oversize','text/plain',11))
    with pytest.raises(BackendError):
        files.path('../secrets')


def test_incomplete_final_preserves_upload_offset_and_hash_failure_removes_file(tmp_path):
    files=FileStore(tmp_path)
    pending=asyncio.run(files.begin('data','text/plain',5))
    with pytest.raises(BackendError) as error:
        asyncio.run(files.append(pending['upload_id'],0,'aGU=',True))
    assert error.value.code=='upload_incomplete'
    assert asyncio.run(files.append(pending['upload_id'],2,'bGxv',True))['size_bytes']==5
    wrong=asyncio.run(files.begin('data','text/plain',1,'0'*64))
    with pytest.raises(BackendError):
        asyncio.run(files.append(wrong['upload_id'],0,'YQ==',True))
    assert not files.path(wrong['upload_id']).exists()


@pytest.mark.parametrize('url',['http://files.oaiusercontent.com/a','https://127.0.0.1/a','https://files.oaiusercontent.com.evil/a','file:///secret','https://user@files.oaiusercontent.com/a'])
def test_upload_ssrf_and_internal_references_are_rejected(tmp_path,url):
    files=FileStore(tmp_path)
    with pytest.raises(BackendError):
        files._remote(url)


def test_dns_private_address_is_rejected_before_connect(tmp_path,monkeypatch):
    files=FileStore(tmp_path)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(0,0,0,'',('127.0.0.1',443))])
    with pytest.raises(BackendError) as error:
        files._remote('https://files.oaiusercontent.com/file')
    assert error.value.code=='unsafe_download_address'


@pytest.fixture
def app_v2(backend,oauth,tmp_path,monkeypatch):
    engine,client=backend
    owner,_,_=oauth
    monkeypatch.setenv('TG_DATA_DIR',str(tmp_path))
    with TestClient(create_app(engine,owner),base_url=owner.public_url) as web:
        yield web,owner,engine,client


def test_chatgpt_file_schema_and_resource_image_content(app_v2):
    web,owner,engine,_=app_v2
    headers=bearer(owner,['telegram.read'])
    tools=rpc(web,headers,'tools/list').json()['result']['tools']
    tool=next(x for x in tools if x['name']=='telegram_upload_file')
    assert tool['_meta']['openai/fileParams']==['file']
    schema=tool['inputSchema']
    file_schema=schema['$defs']['OpenAIFile']
    assert set(file_schema['properties'])=={'download_url','file_id','mime_type','file_name'}
    assert set(file_schema['required'])=={'download_url','file_id'}
    assert file_schema['additionalProperties'] is False
    picture=io.BytesIO()
    Image.new('RGB',(4,5),'blue').save(picture,format='PNG')
    asset=stage(engine,picture.getvalue(),'фото.png','image/png')
    result=rpc(web,headers,'tools/call',{'name':'telegram_get_file','arguments':{'file_id':asset['file_id']}}).json()['result']
    assert any(x['type']=='image' for x in result['content'])
    inline=next(x for x in result['content'] if x['type']=='image')
    assert inline['mimeType']=='image/png' and base64.b64decode(inline['data'])==picture.getvalue()
    assert any(x['type']=='resource_link' for x in result['content'])
    assert result['structuredContent']['original_preserved']
    resource=rpc(web,headers,'resources/read',{'uri':asset['resource_uri']}).json()['result']
    assert base64.b64decode(resource['contents'][0]['blob'])==picture.getvalue()
    assert rpc(web,{},'resources/read',{'uri':asset['resource_uri']}).status_code==401


def test_read_scope_cannot_send_or_manage(app_v2):
    web,owner,engine,client=app_v2
    draft=asyncio.run(engine.prepare_send(None,'Не отправлять'))
    result=rpc(web,bearer(owner,['telegram.read']),'tools/call',{'name':'telegram_execute_action','arguments':{'draft_id':draft['draft_id'],'confirmation_hash':draft['confirmation_hash']}}).json()['result']
    assert result['structuredContent']['error']=='insufficient_scope' and not client.requests


def test_actual_utf8_file_creation_and_superseded_article_cannot_publish(app_v2):
    web,owner,engine,client=app_v2
    headers=bearer(owner,['telegram.read','telegram.send'])
    result=rpc(web,headers,'tools/call',{'name':'telegram_create_text_file','arguments':{'filename':'Документ 👋.md','text':'# Заголовок\n\nТекст','mime_type':'text/markdown'}}).json()['result']['structuredContent']
    assert engine.files.verified_path(result['file_id']).read_bytes()=='# Заголовок\n\nТекст'.encode()
    assert result['filename']=='Документ 👋.md'
    draft=asyncio.run(engine.create_article('Исходная',[{'type':'paragraph','text':'Исходный текст'}]))
    changed=rpc(web,headers,'tools/call',{'name':'telegram_update_article','arguments':{'draft_id':draft['draft_id'],'title':'Новая','blocks':[{'type':'paragraph','text':'Изменённый текст'}]}}).json()['result']['structuredContent']
    assert changed['confirmation_hash']!=draft['confirmation_hash']
    with pytest.raises(BackendError):
        asyncio.run(engine.execute_action(draft['draft_id'],draft['confirmation_hash']))
    assert not client.requests
    management=asyncio.run(engine.prepare_contact({'action':'add','phone':'+79990000000','first_name':'Контакт'}))
    result=rpc(web,bearer(owner,['telegram.read','telegram.send']),'tools/call',{'name':'telegram_execute_management_action','arguments':{'draft_id':management['draft_id'],'confirmation_hash':management['confirmation_hash']}}).json()['result']
    assert result['structuredContent']['error']=='insufficient_scope' and not client.requests


def test_fallback_upload_is_expiring_and_does_not_publish(app_v2):
    web,owner,engine,client=app_v2
    pending=asyncio.run(engine.files.begin('данные.txt','text/plain',5))
    url=owner.public_url+'/upload/'+pending['upload_id']
    assert web.get(url).status_code==200
    assert web.put(url,content=b'hello',headers={'Content-Type':'application/octet-stream','Origin':'https://evil.example'}).status_code==403
    result=web.put(url,content=b'hello',headers={'Content-Type':'application/octet-stream','Origin':owner.origin})
    assert result.status_code==200 and result.json()['sha256']==hashlib.sha256(b'hello').hexdigest()
    assert web.put(url,content=b'hello',headers={'Content-Type':'application/octet-stream'}).status_code==400
    assert not client.requests


@pytest.mark.parametrize('filename,member', [('данные.docx','word/document.xml'),('слайды.pptx','ppt/slides/slide1.xml'),('текст.odt','content.xml'),('таблица.ods','content.xml')])
def test_modern_office_unicode_extraction_without_macro_execution(tmp_path,filename,member):
    path=tmp_path/'binary.bin'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr(member,'<root><p>Кириллица и Unicode 👋</p></root>')
    assert 'Кириллица и Unicode 👋' in read_document(path,filename)['text']


def test_xlsx_staged_binary_path_and_reader_pagination(tmp_path):
    from openpyxl import Workbook
    path=tmp_path/'binary.bin'
    workbook=Workbook()
    workbook.active.append(['Товар','Цена'])
    workbook.active.append(['РЕСАНТА',1234])
    workbook.save(path)
    result=read_document(path,'прайс.xlsx',0,10)
    assert result['next_offset']==10
    assert 'РЕСАНТА' in read_document(path,'прайс.xlsx')['text']


def test_xml_entities_and_unknown_document_formats_are_explicit(tmp_path):
    path=tmp_path/'binary.bin'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('word/document.xml','<!DOCTYPE root [<!ENTITY x SYSTEM "file:///secrets">]><root><p>&x;</p></root>')
    with pytest.raises(BackendError) as error:
        read_document(path,'опасный.docx')
    assert error.value.code=='unsafe_xml'
    with pytest.raises(BackendError) as error:
        read_document(path,'unknown.bin')
    assert error.value.code=='unsupported_document'


def test_transcription_uses_decoded_numpy_not_pyav_paths(tmp_path,monkeypatch):
    import numpy as np
    import media_processing as processing
    model_path=tmp_path/'model'
    model_path.mkdir()
    for name in ('model.bin','config.json','tokenizer.json','vocabulary.txt'):
        (model_path/name).write_bytes(b'fixture')
    monkeypatch.setenv('TG_WHISPER_MODEL',str(model_path))
    monkeypatch.setattr(processing,'probe_media',lambda path:{'duration_seconds':1})
    monkeypatch.setattr(processing,'_run',lambda *a,**kw:np.zeros(16000,dtype='<f4').tobytes())
    class Model:
        def transcribe(self,audio,**kwargs):
            assert isinstance(audio,np.ndarray) and audio.dtype==np.float32 and len(audio)==16000
            return iter([NS(start=0,end=1,text='Проверка')]),NS(language='ru')
    monkeypatch.setattr(processing,'_stt_model',Model())
    assert processing.transcribe(tmp_path/'voice.ogg')['text']=='Проверка'


def test_strict_models_reject_boolean_identity_fields():
    from tool_models import StoryOptions,ContactChange
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ContactChange(action='delete',user_id=True)
    with pytest.raises(ValidationError):
        StoryOptions(audience='selected',selected_user_ids=[True])
