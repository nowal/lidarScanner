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
    assert Image.open(io.BytesIO(thumb)).size == (360, 270)


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
    assert '<script>Kitchen' not in html
    assert '<img' not in html and '/photos/0/' not in html + text
    assert media.link(rec) in html and media.link(rec) in text
    assert '123 Hidden Street' not in html + text
    assert 'Download original USDZ' not in html
    assert 'Private link expires in 30 days.' in html
    rec.scanMedia['photos'] = []
    assert 'Open scan gallery' in ops_email.build_ops_email_html(rec, [], None)
    rec.synopsis = '<script>alert("x")</script>'
    assert '&lt;script&gt;' in ops_email.build_ops_email_html(rec, [], None)



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


@pytest.mark.asyncio
async def test_repeated_immutable_photo_upload_accepts_storage_duplicate(monkeypatch):
    monkeypatch.setattr(settings, 'supabase_url', 'https://storage.example')
    monkeypatch.setattr(settings, 'supabase_service_role_key', 'test')
    code = '409'
    async def post(self, url, **kwargs):
        return httpx.Response(400, json={'statusCode': code, 'error': 'Duplicate'}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    await media.put_bytes('owner/home/photo.jpg', b'jpeg', 'image/jpeg')
    code = '400'
    with pytest.raises(httpx.HTTPStatusError):
        await media.put_bytes('owner/home/photo.jpg', b'jpeg', 'image/jpeg')


@pytest.mark.asyncio
async def test_legacy_scan_keeps_its_existing_original_download(monkeypatch):
    async def no_published(*args, **kwargs): return None
    monkeypatch.setattr(media.scan_uploads, 'transition', no_published)
    rec = record(modelLink={'url': 'https://signed.example/original.usdz'})
    rec.scanMedia = await media.capture(rec)
    assert rec.scanMedia == {}
    await media.prepare_email(rec)
    assert rec.modelLink['url'] == 'https://signed.example/original.usdz'


def dense_archive(path, count=121):
    photo = io.BytesIO(); Image.new('RGB', (768, 400), 'red').save(photo, 'JPEG')
    transform = [0,1,0,0,-1,0,0,0,0,0,1,0,2,4.2,1,1]
    with zipfile.ZipFile(path, 'w') as z:
        for area in [1, 2]:
            frames = [{'id': f'a{area}-f{n}', 'cameraTransform': transform} for n in range(count)]
            z.writestr(f'rooms/room-{area}/rebuild/manifest.json', json.dumps({'frames': frames}))
            for f in frames: z.writestr(f'rooms/room-{area}/rebuild/images/{f["id"]}.jpg', photo.getvalue())
    return transform


def test_dense_photos_have_no_four_frame_cap_and_keep_scope(tmp_path):
    archive = tmp_path / 'dense.zip'; dense_archive(archive)
    rooms = [{'key': f'room-{a}', 'label': f'Area {a}', 'frameIds': [f'a{a}-f{n}' for n in range(121)]} for a in [1,2]]
    photos = media.photo_bytes(archive, {'rooms': rooms})
    assert len(photos) == 242
    assert [p[0]['frameId'] for p in photos[:6]] == ['a1-f0','a2-f0','a1-f1','a2-f1','a1-f2','a2-f2']
    assert photos[0][0]['camera'] == {'position': [2,4.2,1], 'direction': [0,0,-1]}
    assert Image.open(io.BytesIO(photos[0][1])).size == (400,768)
    scoped = media.photo_bytes(archive, {'rooms': rooms[1:]})
    assert len(scoped) == 121 and all(p[0]['roomKey'] == 'room-2' for p in scoped)


@pytest.mark.asyncio
@pytest.mark.parametrize('selection_version', [1, 2])
async def test_v2_capture_is_metadata_only_scoped_and_revision_pinned(monkeypatch, selection_version):
    raw = index()
    for n, room in enumerate(raw['rooms'], 1):
        room['providerFrames'] = [{'id': f'a{n}-f{i}', 'captureArea': 'room-1', 'selectionVersion': selection_version,
            'cameraTransform': [1,0,0,0,0,1,0,0,0,0,1,0,n,1,i,1]} for i in range(12)]
    state = {'revision': REV, 'ownerId': OWNER, 'publishedIndex': raw}
    async def transition(*a, **kw): return state
    async def forbidden(*a, **kw): raise AssertionError('Submission must not prepare dense images')
    monkeypatch.setattr(media.scan_uploads, 'transition', transition)
    monkeypatch.setattr(media.supabase_store, 'download_object', forbidden)
    monkeypatch.setattr(media, 'put_bytes', forbidden)
    monkeypatch.setattr(media, 'queue_conversion', lambda *a: None)
    rec = record(homeownerId=OWNER, scopeIntent='single_room', roomKey='room-2')
    rec.scanMedia = await media.capture(rec)
    snap = copy.deepcopy(rec.scanMedia)
    assert snap['version'] == 2 and snap['photosPrepared']
    assert snap['providerSelectionVersion'] == selection_version
    assert snap['providerSelectionVersions'] == [selection_version]
    assert len(snap['photos']) == 12
    assert all(p['roomKey'] == 'room-2' and p['captureArea'] == 'room-1' for p in snap['photos'])
    assert all('object' not in p for p in snap['photos'])
    await media.prepare_email(rec)
    assert rec.scanMedia['photos'] == snap['photos']
    raw['rooms'][1]['providerFrames'].clear()
    state['revision'] = 'new-revision'
    assert rec.scanMedia['photos'] == snap['photos']
    assert rec.scanMedia['revision'] == REV
    assert await media.photos_with_camera(rec.scanMedia) == snap['photos']


@pytest.mark.asyncio
async def test_on_demand_photo_retries_reuses_receipt_and_reads_pinned_context(tmp_path, monkeypatch):
    archive = tmp_path / 'context.zip'; dense_archive(archive, 12)
    photo = {'frameId': 'a1-f10', 'captureArea': 'room-1', 'roomKey': 'room-2', 'label': 'Upstairs'}
    snap = {'version': 2, 'snapshotId': 'fixed', 'homeId': HOME, 'ownerId': OWNER,
            'context': {'bucket': media.BUCKET, 'object': 'original-context.zip'}}
    objects, downloads = {}, []
    fail = True
    async def get(path): return json.loads(objects[path]) if path in objects else {}
    async def put(path, data, content_type, **kw):
        nonlocal fail
        if '-thumb-' in path and fail:
            fail = False
            raise RuntimeError('retryable upload failure')
        objects[path] = data
    async def download(bucket, path, target, *, max_bytes):
        downloads.append((path, max_bytes)); target.write_bytes(archive.read_bytes()); return True
    async def transition(*a, **kw): return {'ownerId': OWNER, 'revision': 'later-revision'}
    monkeypatch.setattr(media, 'get_json', get)
    monkeypatch.setattr(media, 'put_bytes', put)
    monkeypatch.setattr(media.supabase_store, 'download_object', download)
    monkeypatch.setattr(media.scan_uploads, 'transition', transition)
    with pytest.raises(Exception) as exc: await media.photo_object(snap, photo, 'full')
    assert exc.value.status_code == 503
    assert not any(k.endswith('.json') for k in objects)
    full = await media.photo_object(snap, photo, 'full')
    thumb = await media.photo_object(snap, photo, 'thumb')
    assert len(downloads) == 2 and downloads == [('original-context.zip', 32 * 1024 * 1024)] * 2
    assert Image.open(io.BytesIO(objects[full])).size == (400,768)
    assert Image.open(io.BytesIO(objects[thumb])).size == (360,270)
    assert photo == {'frameId': 'a1-f10', 'captureArea': 'room-1', 'roomKey': 'room-2', 'label': 'Upstairs'}
    assert await media.photo_object(snap, {'object': 'homeowner.jpg', 'thumbnail': 'homeowner-thumb.jpg'}, 'full') == 'homeowner.jpg'


@pytest.mark.asyncio
async def test_v2_status_and_photo_authorization_without_eager_download(monkeypatch):
    rec = await saved_snapshot(monkeypatch)
    rec.scanMedia.update(version=2, photos=[{'label': 'Sample', 'roomKey': 'room-1', 'camera': None,
        'frameId': 'p20', 'captureArea': 'room-1'}, {'label': 'Homeowner', 'supplemental': True,
        'object': 'homeowner.jpg', 'thumbnail': 'homeowner-thumb.jpg'}])
    await quote_store.save(rec)
    exp = rec.scanMedia['expiresAt']; sig = media.signature(rec, exp)
    path = f'/api/v1/quote-media/{rec.id}/{exp}/{sig}'
    prepared = []
    async def prepare(scan, photo, size): prepared.append((scan['snapshotId'], photo, size)); return 'private.jpg'
    async def sign(*a, **kw): return 'https://storage.example/private-signed'
    async def status(*a): return {'status': 'preparing'}
    monkeypatch.setattr(media, 'photo_object', prepare)
    monkeypatch.setattr(media, 'conversion_status', status)
    monkeypatch.setattr(media.supabase_store, '_signed_storage_url', sign)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.get(path + '/status')
        assert response.status_code == 200 and not prepared
        assert response.json()['photos'][1]['supplemental']
        assert all('captureArea' not in p and 'object' not in p for p in response.json()['photos'])
        assert (await c.get(path + '/photos/0/full')).status_code == 307
        assert len(prepared) == 1
        assert response.headers['cache-control'] == 'private, no-store'
        assert (await c.get(path + '/photos/3/full')).status_code == 404
        assert (await c.get(path[:-1] + 'x/photos/0/full')).status_code == 403
        assert len(prepared) == 1


def test_missing_corrupt_oversized_and_unposed_images(tmp_path):
    archive = tmp_path / 'images.zip'
    small = io.BytesIO(); Image.new('RGB', (40,20), 'blue').save(small, 'JPEG')
    large = io.BytesIO(); Image.new('L', (4001,4000)).save(large, 'JPEG')
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('rooms/room-1/rebuild/manifest.json', json.dumps({'frames': [
            {'id': name, 'cameraTransform': [1,2]} for name in ['missing','corrupt','large','unposed']]}))
        z.writestr('rooms/room-1/rebuild/images/corrupt.jpg', b'not a JPEG')
        z.writestr('rooms/room-1/rebuild/images/large.jpg', large.getvalue())
        z.writestr('rooms/room-1/rebuild/images/unposed.jpg', small.getvalue())
    photos = media.photo_bytes(archive, {'rooms': [{'key':'room-1','label':'Area','frameIds':['missing','corrupt','large','unposed']}]})
    assert len(photos) == 1 and photos[0][0]['frameId'] == 'unposed' and photos[0][0]['camera'] is None


def test_capture_area_disambiguates_reused_frame_ids(tmp_path):
    archive = tmp_path / 'duplicate-ids.zip'
    small = io.BytesIO(); Image.new('RGB', (40,20), 'blue').save(small, 'JPEG')
    with zipfile.ZipFile(archive, 'w') as z:
        for area in [1,2]:
            pose = [1,0,0,0,0,1,0,0,0,0,1,0,area,1,0,1]
            z.writestr(f'rooms/room-{area}/rebuild/manifest.json', json.dumps({'frames':[{'id':'same', 'cameraTransform':pose}]}))
            z.writestr(f'rooms/room-{area}/rebuild/images/same.jpg', small.getvalue())
    scope = {'rooms':[{'key':'room-2','label':'Upstairs','frameIds':['same']}], 'captureArea':'room-1'}
    assert media.photo_bytes(archive, scope)[0][0]['camera']['position'] == [1,1,0]
    del scope['captureArea']
    assert media.photo_bytes(archive, scope) == []


@pytest.mark.asyncio
async def test_old_sparse_snapshot_recovers_only_camera_metadata(tmp_path, monkeypatch):
    archive = tmp_path / 'sparse.zip'; dense_archive(archive, 2)
    existing_camera = {'position':[1,2,3], 'direction':[0,0,-1]}
    snap = {'snapshotId':'old','homeId':HOME,'ownerId':OWNER,'context':{'bucket':media.BUCKET,'object':'old-context.zip'},
            'photos':[{'frameId':'a1-f0','object':'old.jpg'}, {'frameId':'a1-f1','camera':existing_camera},
                      {'supplemental':True,'object':'homeowner.jpg'}]}
    downloads = []
    async def download(bucket, path, target, *, max_bytes):
        downloads.append(path); target.write_bytes(archive.read_bytes()); return True
    async def get(*a): return {}
    async def put(*a, **kw): pass
    def forbidden(*a, **kw): raise AssertionError('Pose backfill must not decode image pixels')
    monkeypatch.setattr(media.supabase_store, 'enabled', lambda: True)
    monkeypatch.setattr(media.supabase_store, 'download_object', download)
    monkeypatch.setattr(media, 'get_json', get); monkeypatch.setattr(media, 'put_bytes', put)
    monkeypatch.setattr(Image, 'open', forbidden)
    photos = await media.photos_with_camera(snap)
    assert downloads == ['old-context.zip']
    assert len(photos) == 3 and photos[0]['camera']['position'] == [2,4.2,1]
    assert photos[1]['camera'] == existing_camera and photos[2] == snap['photos'][2]
    assert 'camera' not in snap['photos'][0]
