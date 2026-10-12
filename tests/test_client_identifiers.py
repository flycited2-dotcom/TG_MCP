"""Regression: adjacent Telegram int64 photo IDs collapse in JS JSON numbers."""
import io
import json
from types import SimpleNamespace as NS

from PIL import Image
from telethon.tl import types as t
from telegram_backend import TelegramBackend, _json_safe_numbers
from test_v2 import app_v2, backend, stage
from test_oauth_owner import oauth
from test_server import bearer, rpc


def test_six_native_photo_ids_remain_distinct_in_javascript_clients():
    identifiers=[5361854056302849020+i for i in range(6)]
    assert len(set(float(i) for i in identifiers))==1  # Reproduce observed JS loss.
    message=NS(id=123,message='',sender_id=999,out=True,date=None,file=None,media=None,
               rich_message=NS(blocks=[t.PageBlockPhoto(i,t.PageCaption(t.TextEmpty(),t.TextEmpty())) for i in identifiers],
                               photos=[NS(id=i) for i in identifiers],documents=[NS(id=identifiers[0])]))
    returned=json.loads(json.dumps(TelegramBackend._message(message,999)))
    assert returned['message_id']==123 and returned['chat_id']==999
    expected=[str(i) for i in identifiers]
    assert returned['rich_message']['photo_ids']==expected
    assert [b['photo_id'] for b in returned['rich_message']['blocks']]==expected
    assert returned['rich_message']['document_ids']==[expected[0]]
    assert len(set(returned['rich_message']['photo_ids']))==6


def test_json_safe_boundary_preserves_small_numbers_and_booleans():
    values={'max':9007199254740991,'unsafe':9007199254740992,
            'negative':[-9007199254740992,123,True,None,0.5]}
    assert _json_safe_numbers(values)=={'max':9007199254740991,'unsafe':'9007199254740992',
                                      'negative':['-9007199254740992',123,True,None,0.5]}


def test_image_tool_returns_actual_original_markdown_without_search(app_v2):
    web,owner,engine,_=app_v2
    image=stage(engine, bytes.fromhex('89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c020000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082'),'исходное.png','image/png')
    result=rpc(web,bearer(owner,['telegram.read']),'tools/call',
               {'name':'telegram_get_file','arguments':{'file_id':image['file_id']}}).json()['result']
    info=result['structuredContent']
    assert info['sha256']==image['sha256']
    assert info['display_markdown']=='![Original Telegram image]('+info['download_url']+')'
    assert '/file-downloads/' in info['download_url']
    assert 'Never use image_query/image_group' in info['display_policy']
    assert any(c['type']=='image' for c in result['content'])


def test_original_viewer_uses_standard_ui_bridge_and_restricted_csp(app_v2):
    web,owner,_,_=app_v2
    headers=bearer(owner,['telegram.read'])
    tools=rpc(web,headers,'tools/list').json()['result']['tools']
    tool=next(t for t in tools if t['name']=='telegram_get_attachment')
    uri=tool['_meta']['ui']['resourceUri']
    resource=rpc(web,headers,'resources/read',{'uri':uri}).json()['result']['contents'][0]
    assert resource['mimeType']=='text/html;profile=mcp-app'
    assert resource['_meta']['ui']['csp']['connectDomains']==[]
    assert len(resource['_meta']['ui']['csp']['resourceDomains'])==1
    assert 'ui/initialize' in resource['text'] and 'ui/notifications/tool-result' in resource['text']
    assert 'innerHTML' not in resource['text']
    assert 'publish_article' not in resource['text']


def test_image_tool_corrects_misdeclared_format_without_changing_bytes_or_name(app_v2):
    web,owner,engine,_=app_v2
    original=io.BytesIO()
    Image.new('RGB',(2,2),'blue').save(original,format='JPEG')
    image=stage(engine,original.getvalue(),'original.png','image/png')
    result=rpc(web,bearer(owner,['telegram.read']),'tools/call',
               {'name':'telegram_get_file','arguments':{'file_id':image['file_id']}}).json()['result']
    info=result['structuredContent']
    assert info['mime_type']=='image/jpeg' and info['declared_mime_type']=='image/png'
    assert info['filename']=='original.png' and info['sha256']==image['sha256']
    assert info['preview_uses_original_bytes'] is True
    assert next(c for c in result['content'] if c['type']=='resource_link')['mimeType']=='image/jpeg'
