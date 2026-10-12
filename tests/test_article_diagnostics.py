"""Article capability evidence and protocol errors must not imply acceptance."""
import json

from telethon.errors import RPCError
from telegram_backend import _safe_error
from test_v2 import backend,app_v2
from test_oauth_owner import oauth
from test_server import bearer,rpc


def test_known_rpc_error_is_diagnostic_without_request_data():
    error=RPCError('PRIVATE REQUEST AND TOKEN','RICH_MESSAGE_INVALID',400)
    result=_safe_error(error).as_dict()
    assert result['telegram_rpc_error']=='RICH_MESSAGE_INVALID'
    assert result['telegram_rpc_code']==400
    assert result['telegram_error_type']=='RPCError'
    assert 'PRIVATE' not in json.dumps(result)


def test_unknown_rpc_payload_and_plain_exception_are_not_exposed():
    for error in (RPCError('SECRET REQUEST','SECRET TOKEN',400),ValueError('SECRET TOKEN')):
        result=_safe_error(error).as_dict()
        assert 'SECRET' not in json.dumps(result)
        assert 'telegram_rpc_error' not in result


def test_capability_separates_api_implementation_from_live_acceptance(app_v2):
    web,owner,_,client=app_v2
    headers=bearer(owner,['telegram.read'])
    capabilities=rpc(web,headers,'tools/call',{'name':'telegram_capabilities','arguments':{}}).json()['result']['structuredContent']
    assert capabilities['native_article_api_implemented'] is True
    assert capabilities['native_article_publication_verified'] is False
    status=rpc(web,headers,'tools/call',{'name':'telegram_status','arguments':{}}).json()['result']['structuredContent']
    assert status['article_publication_verified'] is False
    assert 'telegram_publish_article' in status['article_tools']
    assert not client.requests


def test_article_evidence_requires_read_back_and_matches_account(backend):
    engine,_=backend
    path=engine.output_dir.parent/'verification'/'article-retry-results.json'
    path.parent.mkdir()
    record={'status':'sent_and_verified','native_article_verified':True,'read_back_verified':True,'messages_sent':1,
            'result':{'sent':True,'publication_type':'native_article','chat':{'chat_id':999,'type':'saved_messages'},'message_ids':[123]}}
    path.write_text(json.dumps(record))
    assert engine.article_publication_evidence(999)['verified']
    assert not engine.article_publication_evidence(100)['verified']
    record['read_back_verified']=False
    path.write_text(json.dumps(record))
    assert not engine.article_publication_evidence(999)['verified']


def test_six_photo_evidence_requires_unique_originals_and_read_back(backend):
    engine,_=backend
    path=engine.output_dir.parent/'verification'/'business-article-results.json'
    path.parent.mkdir()
    record={'status':'sent_and_verified','native_article_verified':True,'read_back_verified':True,'messages_sent':1,
            'chatgpt_six_original_file_params_verified':True,'source_photo_order_verified':True,
            'files':[{'sha256':str(i)} for i in range(6)],
            'article_blocks':[{'_':'PageBlockPhoto','photo_id':str(5361854056302849020+i)} for i in range(6)],
            'result':{'sent':True,'publication_type':'native_article','chat':{'chat_id':999,'type':'saved_messages'},'message_ids':[123]}}
    path.write_text(json.dumps(record))
    result=engine.article_publication_evidence(999)
    assert result['verified'] and result['six_original_photos_from_chatgpt_verified']
    assert not engine.article_publication_evidence(100)['verified']
    record['article_blocks'][1]['photo_id']=record['article_blocks'][0]['photo_id']
    path.write_text(json.dumps(record))
    assert not engine.article_publication_evidence(999)['verified']
