"""Exercise composed MCP responses against the installed SDK 2 wire contract."""
import base64
import io
import json
import wave

from PIL import Image

from test_oauth_owner import oauth
from test_server import bearer, rpc
from test_v2 import app_v2, backend, stage


def call(app, name, arguments):
    web, owner, _, _ = app
    result = rpc(web, bearer(owner, ['telegram.read', 'telegram.send']), 'tools/call',
                 {'name': name, 'arguments': arguments}).json()['result']
    assert not result.get('isError'), result
    return result


def test_article_preview_contains_all_six_originals_in_exact_block_order(app_v2):
    _, _, engine, client = app_v2
    images = []
    blocks = [{'type': 'paragraph', 'text': 'Перед исходными фото'}]
    for index in range(6):
        data = io.BytesIO()
        Image.new('RGB', (3, 2), (index * 30, 20, 40)).save(data, format='PNG')
        info = stage(engine, data.getvalue(), f'original-{index}.png', 'image/png')
        images.append(info)
        blocks.extend([{'type': 'photo', 'file_id': info['file_id']},
                       {'type': 'paragraph', 'text': f'После фото {index}'}])
    draft = call(app_v2, 'telegram_create_article_draft', {'title': 'Шесть оригиналов', 'blocks': blocks})['structuredContent']
    preview = call(app_v2, 'telegram_preview_article', {'draft_id': draft['draft_id']})
    info = preview['structuredContent']
    assert info['preview']['recipient']['chat_id'] == 999
    assert [p['file_id'] for p in info['preview_images']] == [p['file_id'] for p in images]
    assert [p['sha256'] for p in info['preview_images']] == [p['sha256'] for p in images]
    assert len([part for part in preview['content'] if part['type'] == 'image']) == 6
    assert json.loads(preview['content'][0]['text']) == info
    assert preview['_meta']['ui']['resourceUri'] == 'ui://telegram/original-media-v1.html'
    assert info['state'] == 'prepared' and not client.requests
    assert info['block_count'] == 13 and info['original_photo_count'] == 6
    assert info['presentation']['model_can_observe_rendering'] is False


def test_audio_response_includes_actual_wav_and_updated_metadata(app_v2, monkeypatch):
    _, _, engine, _ = app_v2
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x00\x00' * 80)
    data = audio.getvalue()
    info = stage(engine, data, 'voice.wav', 'audio/wav')
    monkeypatch.setattr('extension_tools.audio_preview', lambda path: (path.read_bytes(), {'preview_seconds': 0.01, 'preview_truncated': False}))
    result = call(app_v2, 'telegram_get_audio', {'file_id': info['file_id']})
    part = next(p for p in result['content'] if p['type'] == 'audio')
    assert part['mimeType'] == 'audio/wav' and base64.b64decode(part['data']) == data
    assert result['structuredContent']['preview_seconds'] == 0.01
    assert json.loads(result['content'][0]['text']) == result['structuredContent']


def test_video_response_includes_ordered_frames_and_timestamps(app_v2, monkeypatch):
    _, _, engine, _ = app_v2
    info = stage(engine, b'video fixture', 'clip.mp4', 'video/mp4')
    data = io.BytesIO()
    Image.new('RGB', (2, 2), 'blue').save(data, format='JPEG')
    samples = [{'timestamp_seconds': n, 'data': data.getvalue()} for n in (0.0, 2.0)]
    monkeypatch.setattr('extension_tools.video_frames', lambda path, count: (samples, {'duration_seconds': 4.0}))
    result = call(app_v2, 'telegram_video_frames', {'file_id': info['file_id'], 'count': 2})
    assert result['structuredContent']['sampled_timestamps_seconds'] == [0.0, 2.0]
    assert len([p for p in result['content'] if p['type'] == 'image']) == 2
    assert json.loads(result['content'][0]['text']) == result['structuredContent']
