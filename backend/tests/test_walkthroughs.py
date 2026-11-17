"""Video media uses the real intake/submission routes; no live providers or mail."""
import copy
import json
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from app import flow_api
from app.flow import walkthroughs, scan_uploads, supabase_store, quote_media
from app.flow_quotes import QuoteRequestStore
from app.home_index import HomeIndex
from app.quote_intake import provider
from test_direct_quote_requests import direct
from test_quote_media import HOME, OWNER, REV
from test_quote_intake import PAINT, mapped_reply, wire


def video_index(revision=REV):
    return HomeIndex([], bundle_id=HOME, media_type='walkthrough',
        video=dict(bucket='metashape-exports', object=f'{OWNER}/{HOME}/{revision}.mov',
                   bytes=64, mimeType='video/quicktime', durationSeconds=120),
        upload=dict(ownerId=OWNER, revision=revision, contextReady=True, videoReady=True, modelsReady=False)).to_json()


@pytest.mark.asyncio
async def test_publish_video_without_rooms_models_or_geometry(monkeypatch):
    calls = []
    async def size(*args): return 64
    async def transition(*args, **kwargs): calls.append((args, kwargs)); return {}
    monkeypatch.setattr(supabase_store, 'stored_object_size', size)
    monkeypatch.setattr(scan_uploads, 'transition', transition)
    body = flow_api.WalkthroughUpload(revision=REV, generation=100, objectPath=video_index()['video']['object'],
        bytes=64, mimeType='video/quicktime', durationSeconds=120)
    assert (await walkthroughs.publish(HOME, body, OWNER))['status'] == 'done'
    payload = calls[0][1]['payload']['index']
    assert payload['rooms'] == []
    assert not {'homeModel', 'meshBounds', 'storeys'} & payload.keys()
    assert payload['upload']['videoReady'] and not payload['upload']['modelsReady']
    assert HomeIndex.from_json(payload).to_json() == payload
    assert calls[0][1]['rpc'] == 'walkthrough_upload_transition'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [dict(bytes=63), dict(objectPath=f'foreign/{HOME}/{REV}.mov'),
    dict(objectPath=f'{OWNER}/{HOME}/../{REV}.mov'), dict(objectPath=f'{OWNER}/{HOME}/different.mov'),
    dict(mimeType='video/mp4')])
async def test_incomplete_or_foreign_video_never_publishes(monkeypatch, change):
    async def size(*args): return 64
    async def forbidden(*args, **kwargs): raise AssertionError('Invalid media must not publish')
    monkeypatch.setattr(supabase_store, 'stored_object_size', size)
    monkeypatch.setattr(scan_uploads, 'transition', forbidden)
    body = dict(revision=REV, generation=100, objectPath=video_index()['video']['object'],
                bytes=64, mimeType='video/quicktime', durationSeconds=120)
    with pytest.raises(HTTPException):
        await walkthroughs.publish(HOME, flow_api.WalkthroughUpload(**(body | change)), OWNER)


@pytest.mark.asyncio
async def test_video_chat_service_quote_and_private_playback_survive_replacement(direct, monkeypatch):
    client, original, upload, scheduled = direct
    upload['publishedIndex'] = video_index()
    captured = []
    async def generate(system, payload):
        captured.append((system, json.loads(payload)))
        return mapped_reply(payload)
    monkeypatch.setattr(provider, 'generate', generate)
    turn = wire(HOME)
    active = await client.post('/api/v1/ai/quote-intake/active', json=dict(homeId=HOME, conversationId=turn['conversationId']))
    assert active.status_code == 200, active.text
    reply = await client.post('/api/v1/ai/quote-intake/turn', json=turn)
    assert reply.status_code == 200 and reply.json()['ready'], reply.text
    assert captured[0][1]['workflow']['mediaType'] == 'walkthrough'
    assert captured[0][1]['workflow']['scanAssociated'] is False
    assert 'not a measured scan' in captured[0][0]
    submission = dict(homeId=HOME, conversationId=turn['conversationId'], expectedRevision=1, uploadRevision=REV)
    submitted = await client.post('/api/v1/ai/quote-intake/submit', json=submission)
    assert submitted.status_code == 200, submitted.text
    receipt = submitted.json()['receipt']
    record = await QuoteRequestStore().get(receipt['quoteRequestId'])
    assert record.serviceType == 'Painting' and record.zip == '01234'
    assert record.measurements == {} and record.scanMedia['models'] == []
    assert record.scanMedia['video'] == video_index()['video']
    assert 'No measured dimensions' in record.synopsis
    assert 'Provider model review:' not in record.synopsis
    assert len(scheduled) == 1
    # Reopening uses ordinary persisted conversation state.
    restored = await client.get(f"/api/v1/ai/quote-intake/{turn['conversationId']}", params={'homeId': HOME})
    assert restored.json()['receipt'] == receipt
    pinned = copy.deepcopy(record.scanMedia)
    next_revision = str(uuid4())
    upload.update(revision=next_revision, publishedIndex=video_index(next_revision))
    assert (await client.post('/api/v1/ai/quote-intake/submit', json=submission)).json()['receipt'] == receipt
    assert (await QuoteRequestStore().get(record.id)).scanMedia == pinned
    signed = []
    async def sign(bucket, path, **kwargs):
        signed.append(path); return 'https://storage.example/private-video'
    monkeypatch.setattr(supabase_store, '_signed_storage_url', sign)
    root = quote_media.link(record).replace('https://example.test', '')
    status = await client.get(root + '/status')
    assert status.status_code == 200 and status.json()['mediaType'] == 'walkthrough'
    assert status.json()['models'] == []
    assert status.json()['projectBrief']['scanFacts'] == []
    playback = await client.get(root + '/video', follow_redirects=False)
    assert playback.status_code == 307 and signed == [pinned['video']['object']]
    assert (await client.get(root.replace(root.rsplit('/', 1)[-1], 'invalid') + '/video')).status_code == 403


@pytest.mark.asyncio
async def test_video_submission_requires_the_current_completed_video_revision(direct):
    client, body, upload, scheduled = direct
    upload['publishedIndex'] = video_index()
    upload['publishedIndex']['upload']['videoReady'] = False
    assert (await client.post('/api/v1/ai/quote-requests/direct', json=body)).status_code == 409
    upload['publishedIndex'] = video_index(str(uuid4()))
    assert (await client.post('/api/v1/ai/quote-requests/direct', json=body)).status_code == 409
    assert scheduled == []


def test_long_video_has_no_artificial_duration_or_byte_cap():
    body = flow_api.WalkthroughUpload(revision=REV, generation=100,
        objectPath=video_index()['video']['object'], bytes=5_000_000_000,
        mimeType='video/quicktime', durationSeconds=7200)
    assert body.durationSeconds == 7200
