import asyncio
import copy
import io
import json
import time
import zipfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from app.config import settings
from app.flow import quote_media as media, ops_email
from app.flow_quotes import QuoteRequestRecord, quote_store
from app.main import app

HOME = '25d800a5-d2ba-4ec0-9dd2-7509a7d28ec9'
OWNER = 'cdeca749-2170-4355-b92f-746e39429c61'
REV = 'ea72c644-e1a9-4aad-935d-4299e595bce5'


def model(key):
    return {'bucket': media.BUCKET, 'object': f'{OWNER}/{HOME}/{key}-' + 'a' * 64 + '.usdz', 'bytes': 100}


def index():
    return {'upload': {'revision': REV, 'modelsReady': True, 'ownerId': OWNER, 'contextObject': f'{OWNER}/{HOME}/context-hash.zip'},
            'homeModel': model('home'), 'rooms': [
        {'key': f'room-{n}', 'name': name, 'model': model(f'room-{n}'), 'frameIds': [f'frame-{n}']}
        for n, name in [(1, 'Kitchen'), (2, 'Living room')]]}


def record(**kw):
    return QuoteRequestRecord(id='qr_media', createdAt='2026-10-06', threadId='t', homeId=HOME,
        firstName='Test', address='123 Hidden Street', **kw)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'storage_dir', str(tmp_path))
    monkeypatch.setattr(settings, 'flow_token_secret', 'test-media-secret')
    monkeypatch.setattr(settings, 'public_base_url', 'https://example.test')
    monkeypatch.setattr(settings, 'ops_quote_cc', '')


@pytest.mark.asyncio
async def test_snapshot_scope_revision_and_no_cross_revision_refresh(monkeypatch):
    raw = index()
    state = {'revision': REV, 'ownerId': OWNER, 'publishedIndex': raw,
             'contextIndex': dict(raw, homeModel=model('wrong-new-model'))}
    async def transition(*a, **k): return state
    monkeypatch.setattr(media.scan_uploads, 'transition', transition)
    rec = record(scopeIntent='selected_rooms', scopeRooms=['Living room', 'Kitchen'], roomKey='room-1')
    rec.scanMedia = await media.capture(rec)
    assert [m['key'] for m in rec.scanMedia['models']] == ['room-2', 'room-1']
    assert rec.scanMedia['rooms'][0]['frameIds'] == ['frame-2']
    old = copy.deepcopy(rec.scanMedia)
    state['publishedIndex'] = dict(raw, homeModel=model('later'))
    assert rec.scanMedia == old
    whole = record(scopeIntent='whole_home', roomKey='room-1')
    assert (await media.capture(whole))['models'][0]['key'] == 'home'
    single = record(scopeIntent='single_room', roomKey='room-2')
    assert [m['key'] for m in (await media.capture(single))['models']] == ['room-2']
    unknown = record(scopeIntent='selected_rooms', scopeRooms=['ambiguous'])
    assert 'reason' in await media.capture(unknown)
    state['revision'] = str(uuid4())
    with pytest.raises(Exception) as exc: await media.capture(single)
    assert exc.value.status_code == 409


def test_extract_selected_frames_upright_and_missing(tmp_path):
    archive = tmp_path / 'context.zip'
    photo = io.BytesIO(); Image.new('RGB', (768, 400), 'red').save(photo, 'JPEG')
    transform = [0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('rooms/room-1/rebuild/manifest.json', json.dumps({'frames': [{'id': 'frame-1', 'cameraTransform': transform}]}))
        z.writestr('rooms/room-1/rebuild/images/frame-1.jpg', photo.getvalue())
    snap = {'rooms': [{'key': 'room-1', 'label': 'Kitchen', 'frameIds': ['frame-1', 'missing']},
                      {'key': 'room-2', 'label': 'Other', 'frameIds': ['missing']}]}
    photos = media.photo_bytes(archive, snap)
    assert len(photos) == 1
    info, full, thumb = photos[0]
    assert info['roomKey'] == 'room-1'
    assert Image.open(io.BytesIO(full)).size == (400, 768)
    assert max(Image.open(io.BytesIO(thumb)).size) <= 360


async def saved_snapshot(monkeypatch):
    async def transition(*a, **kw): return {'revision': REV, 'ownerId': OWNER, 'publishedIndex': index()}
    monkeypatch.setattr(media.scan_uploads, 'transition', transition)
    rec = record(scopeIntent='whole_home')
    rec.scanMedia = await media.capture(rec)
    rec.scanMedia['expiresAt'] = int(time.time()) + 600
    rec.scanMedia['photos'] = [{'label': '<script>Kitchen & "view"</script>', 'object': 'private/full.jpg', 'thumbnail': 'private/thumb.jpg'}]
    await quote_store.save(rec)
    return rec


@pytest.mark.asyncio
async def test_read_only_capability_expiry_scope_and_short_storage_links(monkeypatch):
    rec = await saved_snapshot(monkeypatch)
    exp = rec.scanMedia['expiresAt']; sig = media.signature(rec, exp)
    path = f'/api/v1/quote-media/{rec.id}/{exp}/{sig}'
    signed = []
    async def sign(bucket, key, expires_in):
        signed.append((bucket, key, expires_in)); return 'https://storage.example/signed'
    async def status(*a): return {'status': 'failed'}
    monkeypatch.setattr(media, 'conversion_status', status)
    monkeypatch.setattr(media.supabase_store, '_signed_storage_url', sign)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        assert (await c.get(path)).status_code == 200
        status_result = await c.get(path + '/status')
        assert status_result.json()['models'][0]['status'] == 'failed'
        assert 'object' not in status_result.text and settings.flow_token_secret not in status_result.text
        assert (await c.get(path + '/photos/0/thumb')).status_code == 307
        assert signed[-1] == (media.BUCKET, 'private/thumb.jpg', 300)
        assert (await c.get(path + '/models/0/usdz')).status_code == 307
        assert (await c.get(path + '/models/0/glb')).status_code == 409
        assert (await c.get(path + '/models/1/usdz')).status_code == 404
        assert (await c.get(path + '/photos/-1/full')).status_code == 404
        assert (await c.get(path[:-1] + ('0' if sig[-1] != '0' else '1'))).status_code == 403
        assert (await c.get(path.replace(rec.id, 'qr_other'))).status_code == 404
        assert (await c.post(path)).status_code == 405
        assert (await c.get(f'/api/v1/ops/entry/{rec.id}/{exp}/{sig}')).status_code == 403
        expired = int(time.time()) - 1
        assert (await c.get(f'/api/v1/quote-media/{rec.id}/{expired}/{media.signature(rec, expired)}')).status_code == 410
    # A new process can load the durable snapshot without a context extraction dir.
    from app.flow_quotes import QuoteRequestStore
    cold = await QuoteRequestStore().get(rec.id)
    assert cold.scanMedia['photos'] == rec.scanMedia['photos']


@pytest.mark.asyncio
async def test_email_html_escapes_media_and_handles_missing(monkeypatch):
    rec = await saved_snapshot(monkeypatch)
    rec.modelLink = {'url': media.link(rec) + '/models/0/usdz'}
    html = ops_email.build_ops_email_html(rec, [], None)
    _, text = ops_email.build_ops_email(rec, [], None)
    assert '<script>Kitchen' not in html and '&lt;script&gt;Kitchen &amp;' in html
    assert '/photos/0/thumb' in html and '/photos/0/full' in html
    assert '/photos/0/full' in text and 'desktop' in text.lower()
    assert '123 Hidden Street' not in html + text
    assert 'Download original USDZ' in html
    rec.scanMedia['photos'] = []
    assert 'No scan photos' in ops_email.build_ops_email_html(rec, [], None)


@pytest.mark.asyncio
async def test_cc_recipients_reply_trust_and_resend_idempotency(monkeypatch):
    monkeypatch.setattr(settings, 'ops_email', 'quintin@example.com')
    monkeypatch.setattr(settings, 'ops_quote_cc', 'Noah <noah@takeshapehome.com>, noah@takeshapehome.com, quintin@example.com')
    monkeypatch.setattr(settings, 'ops_reply_enabled', True)
    monkeypatch.setattr(settings, 'ops_reply_to', 'reply@example.com')
    monkeypatch.setattr(settings, 'resend_api_key', 'test')
    requests = []
    async def post(self, url, **kw):
        requests.append(kw); return httpx.Response(200, json={'id': 'sent'}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    await ops_email._send_via_resend('quintin@example.com', 'Subject', 'Body', cc=ops_email.recipient_list(settings.ops_quote_cc), idempotency_key='key')
    assert requests[-1]['json']['to'] == ['quintin@example.com']
    assert requests[-1]['json']['cc'] == ['noah@takeshapehome.com']
    assert requests[-1]['json']['reply_to'] == ['reply@example.com']
    assert requests[-1]['headers']['Idempotency-Key'] == 'key'
    await ops_email.send_ops_message('Other', 'message')
    assert 'cc' not in requests[-1]['json']
    from app.flow.ops_reply import allowed_reply_senders
    assert 'noah@takeshapehome.com' not in allowed_reply_senders()
    with pytest.raises(ValueError): ops_email.recipient_list('a@b.com\nBcc: stolen@example.com')


@pytest.mark.asyncio
async def test_retry_uses_same_payload_and_does_not_duplicate(monkeypatch):
    monkeypatch.setattr(settings, 'ops_email', 'quintin@example.com')
    monkeypatch.setattr(settings, 'ops_quote_cc', 'noah@takeshapehome.com')
    rec = record(); rec.homeId = None
    attempts = []
    async def send(*args, **kwargs):
        attempts.append((args, kwargs)); return 'failed' if len(attempts) == 1 else 'sent'
    monkeypatch.setattr(ops_email, 'send_ops_message', send)
    assert await ops_email.send_ops_email(rec) == 'failed'
    cold = await quote_store.get(rec.id)
    assert await ops_email.send_ops_email(cold) == 'sent'
    assert attempts[0] == attempts[1]
    stale = record(); stale.homeId = None
    assert await ops_email.send_ops_email(stale) == 'already_sent'
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_conversion_failure_cooldown_and_restart_queue(monkeypatch):
    rec = await saved_snapshot(monkeypatch)
    m = rec.scanMedia['models'][0]
    state = {'status': 'failed', 'updatedAt': time.time()}
    async def get_json(*a): return state
    queued = []
    monkeypatch.setattr(media, 'get_json', get_json)
    monkeypatch.setattr(media, 'queue_conversion', lambda *args: queued.append(args))
    assert (await media.conversion_status(rec.scanMedia, m))['status'] == 'failed'
    assert not queued
    state.update(status='preparing', updatedAt=0)
    assert (await media.conversion_status(rec.scanMedia, m))['status'] == 'preparing'
    assert len(queued) == 1
    state['status'] = 'ready'
    assert (await media.conversion_status(rec.scanMedia, m))['status'] == 'ready'
    assert len(queued) == 1
