"""Grok media -> Telegram staging over the owner's computer bridge: verified bytes, idempotency, no manual download."""
import asyncio
import hashlib
import io

import pytest
from PIL import Image
from starlette.testclient import TestClient
from telethon.tl import functions as f,types as t

import media_processing
from extended_backend import ExtendedBackend
from server import create_app
from test_server import bearer,rpc
from test_oauth_owner import oauth
from test_v2 import Client

TOKEN='b'*40
MP4=b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom'+b'\x00'*2000


@pytest.fixture
def web(tmp_path,oauth,monkeypatch):
    client=Client()
    engine=ExtendedBackend(client=client,allow_send=True,output_dir=tmp_path/'output',drafts_db=tmp_path/'drafts.sqlite')
    token=tmp_path/'staged-token'
    token.write_text(TOKEN)
    monkeypatch.setenv('TG_MEDIA_BRIDGE_TOKEN_FILE',str(token))
    monkeypatch.setenv('TG_DATA_DIR',str(tmp_path))
    monkeypatch.setattr(media_processing,'probe_media',lambda path:{'duration_seconds':6,'width':720,'height':1280})
    owner,_,_=oauth
    with TestClient(create_app(engine,owner),base_url=owner.public_url) as app:
        yield app,owner,engine,client,token


def tool(app,owner,name,arguments,scopes=('telegram.read','telegram.send')):
    return rpc(app,bearer(owner,list(scopes)),'tools/call',{'name':name,'arguments':arguments}).json()['result']['structuredContent']


def bridge(token=TOKEN):
    return {'Authorization':'Bearer '+token}


def push(app,import_id,data,sha=None,headers=None):
    return app.put('/media-bridge/upload/'+import_id,content=data,headers={**bridge(),'X-Content-SHA256':sha or hashlib.sha256(data).hexdigest(),'Content-Type':'application/octet-stream',**(headers or {})})


def start(app,owner,asset='grok-asset-1',**extra):
    result=tool(app,owner,'telegram_import_grok_media',{'asset_id':asset,'wait_s':0,**extra})
    assert result['state']=='pending' and result['import_id'].startswith('imp_'),result
    return result


def test_mp4_travels_from_the_bridge_to_saved_messages_as_a_playable_video(web):
    app,owner,engine,client,_=web
    started=start(app,owner,filename='Ресанта СПН-13500.mp4',expected_size=len(MP4))
    listed=app.get('/media-bridge/requests',headers=bridge()).json()['requests']
    assert listed==[{'import_id':started['import_id'],'asset_id':'grok-asset-1','filename':'Ресанта СПН-13500.mp4','expected_size':len(MP4),'expected_sha256':None}]
    assert not client.requests
    delivered=push(app,started['import_id'],MP4)
    assert delivered.status_code==200 and delivered.json()['sha256']==hashlib.sha256(MP4).hexdigest()
    ready=tool(app,owner,'telegram_import_status',{'import_id':started['import_id']})
    assert ready['success'] and ready['ready_for_send'] and ready['media_kind']=='video' and ready['mime_type']=='video/mp4' and ready['size_bytes']==len(MP4)
    assert ready['filename'].endswith('.mp4') and 'telegram_prepare_send' in ready['next_step'] and ready['correlation_id']==started['import_id']
    assert engine.files.verified_path(ready['file_id']).read_bytes()==MP4
    assert app.get('/media-bridge/requests',headers=bridge()).json()['requests']==[]
    draft=tool(app,owner,'telegram_prepare_send',{'chat_id':None,'text':'РЕСАНТА СПН-13500 — рекламный ролик. 6 секунд.','media':[{'file_id':ready['file_id'],'kind':'video','original':False}]})
    assert draft['preview']['recipient']['type']=='saved_messages' and draft['preview']['media'][0]['sha256']==hashlib.sha256(MP4).hexdigest() and not client.requests
    sent=tool(app,owner,'telegram_execute_action',{'draft_id':draft['draft_id'],'confirmation_hash':draft['confirmation_hash']})
    assert sent['sent'] is True and sent['message_ids']==[123] and sent['chat']['type']=='saved_messages'
    request=client.requests[-1]
    assert isinstance(request,f.messages.SendMediaRequest) and isinstance(request.peer,t.InputPeerSelf)
    assert isinstance(request.media,t.InputMediaUploadedDocument) and not request.media.force_file and request.media.mime_type=='video/mp4'
    assert any(isinstance(a,t.DocumentAttributeVideo) and a.supports_streaming for a in request.media.attributes)
    again=tool(app,owner,'telegram_execute_action',{'draft_id':draft['draft_id'],'confirmation_hash':draft['confirmation_hash']})
    assert again['already_executed'] and len([r for r in client.requests if isinstance(r,f.messages.SendMediaRequest)])==1


def test_repeating_an_import_returns_the_same_one_without_copies(web):
    app,owner,engine,client,_=web
    first=start(app,owner)
    second=tool(app,owner,'telegram_import_grok_media',{'asset_id':'grok-asset-1','wait_s':0})
    assert second['import_id']==first['import_id'] and len(app.get('/media-bridge/requests',headers=bridge()).json()['requests'])==1
    push(app,first['import_id'],MP4)
    repeat=tool(app,owner,'telegram_import_grok_media',{'asset_id':'grok-asset-1','wait_s':0})
    assert repeat['import_id']==first['import_id'] and repeat['state']=='ready'
    assert push(app,first['import_id'],MP4).status_code==409
    other=tool(app,owner,'telegram_import_grok_media',{'asset_id':'grok-asset-1','idempotency_key':'second-try','wait_s':0})
    assert other['import_id']!=first['import_id']


def test_bridge_endpoints_need_the_separate_bridge_token(web):
    app,owner,engine,client,token=web
    started=start(app,owner)
    for headers in ({},{'Authorization':'Bearer wrong-token-wrong-token-wrong-token-12'},{'Authorization':TOKEN}):
        assert app.get('/media-bridge/requests',headers=headers).status_code==401
        assert app.put('/media-bridge/upload/'+started['import_id'],content=MP4,headers={**headers,'X-Content-SHA256':hashlib.sha256(MP4).hexdigest()}).status_code==401
        assert app.post('/media-bridge/fail/'+started['import_id'],json={'code':'SOURCE_EXPIRED'},headers=headers).status_code==401
    access=bearer(owner,['telegram.read'])['Authorization']
    assert app.get('/media-bridge/requests',headers={'Authorization':access}).status_code==401
    token.unlink()
    assert app.get('/media-bridge/requests',headers=bridge()).status_code==401
    refused=tool(app,owner,'telegram_import_grok_media',{'asset_id':'grok-asset-2','wait_s':0})
    assert refused['error']=='TRANSFER_UNAUTHORIZED' and not refused['success']


@pytest.mark.parametrize('case',['sha','size','type','truncated','limit'])
def test_bad_transfers_are_discarded_with_clear_codes(web,case):
    app,owner,engine,client,_=web
    started=start(app,owner,expected_size=len(MP4) if case=='size' else None)
    if case=='sha':
        response=push(app,started['import_id'],MP4,sha='0'*64)
        expected='CHECKSUM_MISMATCH'
    elif case=='size':
        response=push(app,started['import_id'],MP4[:-10])
        expected='CHECKSUM_MISMATCH'
    elif case=='type':
        response=push(app,started['import_id'],b'#!/bin/sh\necho not a video\n'*20)
        expected='INVALID_MEDIA_TYPE'
    elif case=='truncated':
        def short():
            yield MP4[:100]
        response=app.put('/media-bridge/upload/'+started['import_id'],content=short(),headers={**bridge(),'X-Content-SHA256':hashlib.sha256(MP4).hexdigest(),'Content-Length':str(len(MP4))})
        expected='TRANSFER_INCOMPLETE'
    else:
        engine.files.max_bytes=100
        response=push(app,started['import_id'],MP4)
        expected='FILE_TOO_LARGE'
    assert response.status_code>=400
    status=tool(app,owner,'telegram_import_status',{'import_id':started['import_id']})
    assert status['state']=='failed' and status['error']==expected and not status['success'] and 'message' in status
    with engine.files._db() as db:
        assert db.execute("SELECT count(*) FROM files WHERE state='ready'").fetchone()[0]==0
    db.close()
    assert not client.requests


@pytest.mark.parametrize('fmt,mime',[('PNG','image/png'),('JPEG','image/jpeg'),('WEBP','image/webp')])
def test_images_become_photos(web,fmt,mime):
    app,owner,engine,client,_=web
    picture=io.BytesIO()
    Image.new('RGB',(8,8),'red').save(picture,format=fmt)
    started=start(app,owner,asset='img-'+fmt.lower(),filename='карточка.bin')
    push(app,started['import_id'],picture.getvalue())
    ready=tool(app,owner,'telegram_import_status',{'import_id':started['import_id']})
    assert ready['media_kind']=='photo' and ready['mime_type']==mime and ready['filename'].startswith('карточка.')


def test_the_bridge_can_report_a_missing_file_and_old_requests_expire(web):
    app,owner,engine,client,_=web
    started=start(app,owner)
    assert app.post('/media-bridge/fail/'+started['import_id'],json={'code':'SOURCE_EXPIRED'},headers=bridge()).json()=={'ok':True}
    status=tool(app,owner,'telegram_import_status',{'import_id':started['import_id']})
    assert status['error']=='SOURCE_EXPIRED' and 'Download it again' in status['message']
    late=start(app,owner,asset='slow-asset')
    store=engine.media_imports
    real=store.clock
    store.clock=lambda:real()+3600
    expired=tool(app,owner,'telegram_import_status',{'import_id':late['import_id']})
    assert expired['state']=='failed' and expired['error']=='SOURCE_UNAVAILABLE'
    retried=tool(app,owner,'telegram_import_grok_media',{'asset_id':'slow-asset','wait_s':0})
    assert retried['import_id']==late['import_id'] and retried['state']=='pending'


def test_inputs_are_validated(web):
    app,owner,engine,client,_=web
    assert tool(app,owner,'telegram_import_grok_media',{'asset_id':'../../etc/passwd','wait_s':0})['error']=='INVALID_INPUT'
    assert tool(app,owner,'telegram_import_status',{'import_id':'imp_nope'})['error']=='NOT_FOUND'
    assert app.put('/media-bridge/upload/imp_unknownunknownunknown',content=MP4,headers={**bridge(),'X-Content-SHA256':hashlib.sha256(MP4).hexdigest()}).status_code==409
    started=start(app,owner)
    assert app.put('/media-bridge/upload/'+started['import_id'],content=MP4,headers=bridge()).status_code==400


def test_waiting_import_returns_as_soon_as_the_bridge_delivers(web):
    app,owner,engine,client,_=web
    store=engine.media_imports
    row=store.request('grok','async-asset',None,None,None,'default','local')

    async def scenario():
        async def deliver():
            await asyncio.sleep(0.3)
            store._set(row['id'],state='ready',file_id='x'*32,filename='a.mp4',mime='video/mp4',kind='video',size=1,sha256='0'*64)
        task=asyncio.create_task(deliver())
        deadline=asyncio.get_running_loop().time()+5
        while store._row(row['id'])['state']!='ready' and asyncio.get_running_loop().time()<deadline:
            await asyncio.sleep(0.1)
        await task
        return store.view(store._row(row['id']))
    assert asyncio.run(scenario())['state']=='ready'
