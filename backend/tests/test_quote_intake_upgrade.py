"""Release compatibility and photo isolation; all external traffic is captured."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from app import flow_api
from app.flow import quote_media
from app.flow import ops_email
from app.flow_quotes import quote_store
from app.quote_intake import api, engine, provider, rubrics, store
from app.quote_intake.schema import SupplementalPhotoUpload
from test_direct_quote_requests import direct
from test_quote_intake import answer, item, mapped_reply, result, state_for, wire


def photo():
    output = io.BytesIO()
    Image.new('RGB', (32, 24), 'blue').save(output, format='PNG')
    return dict(id=str(uuid4()), fileName='detail.png', mimeType='image/png',
                dataBase64=base64.b64encode(output.getvalue()).decode())


def test_deployed_rubric_snapshot_retains_questions_hash_and_readiness():
    state = state_for()
    pinned = json.loads((Path(__file__).parent / 'fixtures/quote_intake/pinned_v1_rubric.json').read_text())
    original = copy.deepcopy(pinned)
    state['rubric'] = pinned
    updated = engine.apply_result(state, result())
    assert updated['ready'] and not updated['consultationReady']
    assert updated['rubric'] == original
    assert 'Landscaping' not in json.loads(engine.model_input(state, 'America/New_York'))['supportedCatalogLabels']
    assert engine.provider_package(updated)['details']['rubricHash'] == original['content_hash']


def test_legacy_agent_catalog_is_unchanged():
    from app.home_guide_tools import KNOWN_SERVICE_TYPES as legacy
    assert len(legacy) == 13 and 'Landscaping' not in legacy
    assert rubrics.current_snapshot().service('landscaping').catalog_service == 'Landscaping'


@pytest.mark.asyncio
async def test_three_photo_payload_ceiling_does_not_raise_legacy_request_limit(direct):
    client, _, _, _ = direct
    # A deliberately invalid body should reach schema validation at the size
    # required by three 6 MB base64 photos. Other routes retain their old cap.
    headers = {'Content-Length': '24010000'}
    assert (await client.post('/api/v1/ai/quote-intake/turn', json={}, headers=headers)).status_code == 422
    assert (await client.post('/api/v1/ai/quote-requests/direct', json={}, headers=headers)).status_code == 413
    headers['Content-Length'] = '26000001'
    assert (await client.post('/api/v1/ai/quote-intake/turn', json={}, headers=headers)).status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize('completed', [False, True])
async def test_deployed_text_turn_identity_survives_upgrade(direct, monkeypatch, completed):
    client, body, upload, _ = direct
    request = wire(body['homeId'])
    state = state_for(request['text'])
    state['transcript'][0]['id'] = request['clientTurnId']
    state['pendingTurn'] = None if completed else request
    state['completed'] = {request['clientTurnId']: {'request': request}} if completed else {}
    state['revision'] = 1 if completed else 0
    await store.save(request['conversationId'], upload['ownerId'], body['homeId'], state, None)
    async def generate(system, payload): return mapped_reply(payload)
    monkeypatch.setattr(provider, 'generate', generate)
    response = await client.post('/api/v1/ai/quote-intake/turn', json={**request, 'attachments': []})
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_deployed_intake_receipt_fingerprint_survives_upgrade(direct):
    client, body, _, _ = direct
    body['intakeDetails'] = engine.provider_package(engine.apply_result(state_for(), result()))['details']
    body['intakeDetails'].pop('handoffType')
    body['intakeDetails'].pop('supplementalPhotos')
    parsed = flow_api.DirectQuoteSubmission.model_validate(body)
    old_bytes = parsed.model_dump_json(exclude={'intakeDetails': {'handoffType', 'supplementalPhotos'}})
    response = await client.post('/api/v1/ai/quote-requests/direct', json=body)
    assert response.status_code == 201
    saved = await quote_store.get(response.json()['quoteRequestId'])
    saved.submissionFingerprint = hashlib.sha256(old_bytes.encode()).hexdigest()
    await quote_store.save(saved)
    retry = await client.post('/api/v1/ai/quote-requests/direct', json=body)
    assert retry.status_code == 200, retry.text
    assert retry.json()['quoteRequestId'] == saved.id


@pytest.mark.asyncio
async def test_interrupted_photo_turn_recovers_without_bytes_or_duplicate_upload(direct, monkeypatch):
    client, body, upload, _ = direct
    request = wire(body['homeId'])
    request['attachments'] = [photo()]
    writes = []
    async def save(*args): writes.append(args); return 'https://fixture.invalid/private'
    async def fail(*args): raise RuntimeError('interrupted model')
    monkeypatch.setattr(api.supabase_store, 'upload_bytes', save)
    monkeypatch.setattr(provider, 'generate', fail)
    first = await client.post('/api/v1/ai/quote-intake/turn', json=request)
    assert first.status_code == 503
    recovered = await client.get(f"/api/v1/ai/quote-intake/{request['conversationId']}?homeId={body['homeId']}")
    assert recovered.status_code == 200
    pending = recovered.json()['pendingTurn']
    assert 'dataBase64' not in json.dumps(pending)
    async def generate(system, payload):
        assert 'attachments' not in payload and 'storagePath' not in payload and 'dataBase64' not in payload
        return mapped_reply(payload)
    monkeypatch.setattr(provider, 'generate', generate)
    retry = await client.post('/api/v1/ai/quote-intake/turn', json=pending)
    assert retry.status_code == 200, retry.text
    assert len(writes) == 2
    assert len(retry.json()['messages'][0]['attachments']) == 1
    assert (await client.post('/api/v1/ai/quote-intake/turn', json=request)).status_code == 200
    submitted = await client.post('/api/v1/ai/quote-intake/submit', json=dict(
        conversationId=request['conversationId'], homeId=body['homeId'], expectedRevision=1,
        uploadRevision=body['revision']))
    assert submitted.status_code == 200, submitted.text
    record = await quote_store.get(submitted.json()['receipt']['quoteRequestId'])
    assert len(record.scanMedia['photos']) == 1
    assert record.scanMedia['photos'][0]['supplemental']
    saved = await store.read(request['conversationId'], upload['ownerId'], body['homeId'])
    assert 'dataBase64' not in json.dumps(saved)


@pytest.mark.asyncio
async def test_photo_fingerprints_cannot_create_new_turns(direct):
    client, body, _, _ = direct
    request = wire(body['homeId'])
    attachment = photo(); attachment.pop('dataBase64'); attachment['sha256'] = 'a' * 64
    request['attachments'] = [attachment]
    assert (await client.post('/api/v1/ai/quote-intake/turn', json=request)).status_code == 422


@pytest.mark.asyncio
async def test_consultation_submission_recovers_lost_receipt_without_changing_type(direct, monkeypatch):
    client, body, upload, _ = direct
    text = "Paint my living room. I'm not sure which surfaces. ZIP 01234."
    template = result([item(answers=[
        answer('desired_outcome', 'paint', 'Paint'),
        answer('target_scope', 'living room', 'living room'),
        answer('included_surfaces', None, "I'm not sure which surfaces", 'unknown'),
    ], assessment=True)])
    async def generate(system, payload): return mapped_reply(payload, template)
    monkeypatch.setattr(provider, 'generate', generate)
    request = wire(body['homeId'], text=text)
    reply = await client.post('/api/v1/ai/quote-intake/turn', json=request)
    assert reply.json()['consultationReady'] and not reply.json()['ready']
    submit = dict(conversationId=request['conversationId'], homeId=body['homeId'],
                  expectedRevision=1, uploadRevision=body['revision'])
    original = api.submit_direct_quote_request
    async def lose_success(*args):
        await original(*args)
        raise api.HTTPException(503, 'Lost response after durable receipt')
    monkeypatch.setattr(api, 'submit_direct_quote_request', lose_success)
    assert (await client.post('/api/v1/ai/quote-intake/submit', json=submit)).status_code == 503
    monkeypatch.setattr(api, 'submit_direct_quote_request', original)
    upload['revision'] = str(uuid4())
    recovered = await client.post('/api/v1/ai/quote-intake/submit', json=submit)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()['receipt'] and not recovered.json()['consultationReady']
    records = await quote_store.list()
    assert len(records) == 1
    assert records[0].quoteDraft['intake']['handoffType'] == 'consultation_request'


@pytest.mark.asyncio
async def test_photo_total_limit_is_enforced_before_accepting_turn(direct, monkeypatch):
    client, body, upload, _ = direct
    request = wire(body['homeId'])
    request['attachments'] = [photo()]
    state = engine.initial_state()
    state['supplementalPhotos'] = [dict(id=str(uuid4())) for _ in range(12)]
    await store.save(request['conversationId'], upload['ownerId'], body['homeId'], state, None)
    response = await client.post('/api/v1/ai/quote-intake/turn', json=request)
    assert response.status_code == 422 and 'twelve' in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['owner', 'home', 'bucket', 'thumbnail', 'traversal'])
async def test_direct_submission_rejects_foreign_photo_references(direct, mutation):
    client, body, upload, scheduled = direct
    details = engine.provider_package(engine.apply_result(state_for(), result()))['details']
    identifier = str(uuid4())
    path = f"{upload['ownerId']}/{body['homeId']}/quote-intake/{uuid4()}/{identifier}-{'a' * 64}.jpg"
    ref = dict(id=identifier, fileName='detail.jpg', mimeType='image/jpeg', byteCount=123,
               storageBucket=quote_media.BUCKET, storagePath=path, thumbnailPath=path[:-4] + '-thumb.jpg')
    if mutation == 'owner': ref['storagePath'] = path.replace(upload['ownerId'], str(uuid4()))
    if mutation == 'home': ref['storagePath'] = path.replace(body['homeId'], str(uuid4()))
    if mutation == 'bucket': ref['storageBucket'] = 'other-private-bucket'
    if mutation == 'thumbnail': ref['thumbnailPath'] = 'other-owner/private.jpg'
    if mutation == 'traversal': ref['storagePath'] = path.replace('/quote-intake/', '/quote-intake/../')
    details['supplementalPhotos'] = [ref]
    response = await client.post('/api/v1/ai/quote-requests/direct', json={**body, 'intakeDetails': details})
    assert response.status_code == 422, response.text
    assert await quote_store.list() == [] and scheduled == []


@pytest.mark.asyncio
async def test_decompression_bomb_rejected_before_image_conversion(monkeypatch):
    def oversized(*args): raise Image.DecompressionBombError('too large')
    monkeypatch.setattr(api.Image, 'open', oversized)
    with pytest.raises(api.HTTPException) as error:
        await api.store_supplemental_photos([SupplementalPhotoUpload.model_validate(photo())], 'owner', 'home', 'conversation')
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_pending_turn_clears_consultation_readiness_and_covers_model_lease(direct, monkeypatch):
    client, body, upload, _ = direct
    request = wire(body['homeId'])
    state = engine.initial_state(); state['consultationReady'] = True
    await store.save(request['conversationId'], upload['ownerId'], body['homeId'], state, None)
    async def generate(*args):
        saved = await store.read(request['conversationId'], upload['ownerId'], body['homeId'])
        assert not saved['state']['consultationReady']
        assert saved['state']['leaseUntil'] - api.time.time() > 3 * api.settings.anthropic_request_timeout_seconds
        raise RuntimeError('model offline')
    monkeypatch.setattr(provider, 'generate', generate)
    assert (await client.post('/api/v1/ai/quote-intake/turn', json=request)).status_code == 503
    recovered = await client.get(f"/api/v1/ai/quote-intake/{request['conversationId']}?homeId={body['homeId']}")
    assert not recovered.json()['consultationReady']


@pytest.mark.asyncio
async def test_failed_support_email_is_retried_without_restarting(direct, monkeypatch):
    import asyncio
    client, body, _, _ = direct
    created = await client.post('/api/v1/ai/quote-requests/direct', json=body)
    record = await quote_store.get(created.json()['quoteRequestId'])
    monkeypatch.setattr(api.settings, 'ops_email', 'support@example.test')
    monkeypatch.setattr(api.settings, 'resend_api_key', 'mocked-no-network')
    monkeypatch.setattr(ops_email, 'OPS_EMAIL_RETRY_SECONDS', 0.01)
    monkeypatch.setattr(ops_email, '_queue', None)
    attempts = []
    delivered = asyncio.Event()
    async def send(subject, text, html, **kwargs):
        attempts.append(kwargs['idempotency_key'])
        if len(attempts) == 1: return 'failed'
        return 'sent'
    async def capture_media(*args): pass
    original = ops_email._mark_delivered
    async def mark(sent):
        await original(sent)
        delivered.set()
    monkeypatch.setattr(quote_media, 'prepare_email', capture_media)
    monkeypatch.setattr(ops_email, 'send_ops_message', send)
    monkeypatch.setattr(ops_email, '_mark_delivered', mark)
    worker = asyncio.create_task(ops_email.run_ops_email_worker())
    try:
        await asyncio.wait_for(delivered.wait(), timeout=3)
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        assert (await quote_store.get(record.id)).opsEmailDeliveredAt
        assert await ops_email.send_ops_email(record) == 'already_sent'
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError): await worker
