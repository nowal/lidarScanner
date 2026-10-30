"""Provider PDF -> durable receipt -> authorized homeowner, without any email."""
import asyncio
import copy
import io
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from app.config import settings
from app.flow import provider_responses, quote_media as media
from app.flow_api import _record_decision
from app.flow_quotes import quote_store, QuoteRequestStore
from app.main import app
from app.quote_media_api import upload_token
from test_quote_media import saved_snapshot


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'storage_dir', str(tmp_path))
    monkeypatch.setattr(settings, 'flow_token_secret', 'pdf-response-test-secret')
    monkeypatch.setattr(settings, 'public_base_url', 'https://example.test')


@pytest.fixture
def pdf():
    result = io.BytesIO()
    Image.new('RGB', (100, 100), 'white').save(result, 'PDF')
    return result.getvalue()


async def setup(monkeypatch):
    record = await saved_snapshot(monkeypatch)
    exp = record.scanMedia['expiresAt']
    sig = media.signature(record, exp)
    path = f'/api/v1/quote-media/{record.id}/{exp}/{sig}'
    uploaded = []
    async def put(path, data, content_type): uploaded.append((path, data, content_type))
    async def signed(bucket, path, expires_in):
        assert expires_in == 300
        return f'https://storage.example/{path}?fresh=true'
    monkeypatch.setattr(media, 'put_bytes', put)
    monkeypatch.setattr(media.supabase_store, '_signed_storage_url', signed)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    params = {'submission_id': str(uuid4()), 'provider_name': 'Brightline Painting', 'file_name': 'estimate.pdf', 'price_usd': '2450'}
    headers = {'Content-Type': 'application/pdf', 'X-Upload-Token': upload_token(sig)}
    return record, client, path, params, headers, uploaded


@pytest.mark.asyncio
async def test_priced_pdf_response_returns_to_homeowner_and_survives_restart(monkeypatch, pdf):
    rec, client, path, params, headers, uploaded = await setup(monkeypatch)
    async with client:
        response = await client.post(path + '/responses', params=params, headers=headers, content=pdf)
        assert response.status_code == 201, response.text
        assert uploaded[0][0].startswith(media.prefix(rec.scanMedia))
        assert uploaded[0][1:] == (pdf, 'application/pdf')
        view = await client.get(f'/api/v1/ai/quote-requests/{rec.id}')
        assert view.status_code == 200
        quote = view.json()['quotes'][0]
        assert quote['providerName'] == 'Brightline Painting'
        assert quote['priceUsd'] == 2450
        assert quote['document']['fileName'] == 'estimate.pdf'
        assert quote['document']['url'].endswith('?fresh=true')
        assert 'storagePath' not in view.text and 'storageBucket' not in view.text
        assert '123 Hidden Street' not in view.text
        assert view.headers['cache-control'] == 'private, no-store'
        # Retry the same upload: one receipt, no extra storage upload.
        assert (await client.post(path + '/responses', params=params, headers=headers, content=pdf)).status_code == 200
        assert len(uploaded) == 1
    cold = await QuoteRequestStore().get(rec.id)
    assert len(cold.quotes) == 1 and cold.status == 'quotes_ready'
    assert (await quote_store.list(status='quotes_ready'))[0].id == rec.id


@pytest.mark.asyncio
async def test_multiple_simultaneous_providers_and_stale_parent_save(monkeypatch, pdf):
    rec, client, path, params, headers, _ = await setup(monkeypatch)
    stale = copy.deepcopy(rec)
    async with client:
        results = await asyncio.gather(*[
            client.post(path + '/responses', params=dict(params, submission_id=str(uuid4()), provider_name=name),
                        headers=headers, content=pdf)
            for name in ['Brightline', 'Harbor']])
        assert [r.status_code for r in results] == [201, 201]
        await quote_store.save(stale)
        result = (await client.get(f'/api/v1/ai/quote-requests/{rec.id}')).json()
        assert {q['providerName'] for q in result['quotes']} == {'Brightline', 'Harbor'}
        first = result['quotes'][0]['id']
        current = await quote_store.get(rec.id)
        await _record_decision(current, first, 'declined', 'Timing')
        current = await quote_store.get(rec.id)
        assert current.quote_by_id(first).decision == 'declined'
        monkeypatch.setattr(settings, 'ops_token', 'ops-test')
        deleted = await client.delete(f'/api/v1/ops/quote-requests/{rec.id}/quotes/{first}', headers={'Authorization': 'Bearer ops-test'})
        assert deleted.status_code == 200
        assert len((await quote_store.get(rec.id)).quotes) == 1


@pytest.mark.asyncio
async def test_upload_auth_validation_closed_and_no_false_success(monkeypatch, pdf):
    rec, client, path, params, headers, uploaded = await setup(monkeypatch)
    async with client:
        assert (await client.post(path + '/responses', params=params, content=pdf)).status_code == 403
        bad = path.rsplit('/', 1)[0] + '/' + '0' * 64
        assert (await client.post(bad + '/responses', params=params, headers=headers, content=pdf)).status_code == 403
        assert (await client.post(path + '/responses', params=params, headers=headers, content=b'not a PDF')).status_code == 422
        assert (await client.post(path + '/responses', params=dict(params, provider_name='  '), headers=headers, content=pdf)).status_code == 422
        assert (await client.post(path + '/responses', params=dict(params, price_usd='NaN'), headers=headers, content=pdf)).status_code == 422
        assert (await client.post(path + '/responses', params=params, headers=dict(headers, **{'Content-Type': 'text/plain'}), content=pdf)).status_code == 415
        monkeypatch.setattr(provider_responses, 'MAX_PDF_BYTES', 10)
        assert (await client.post(path + '/responses', params=params, headers=headers, content=pdf)).status_code == 413
        monkeypatch.setattr(provider_responses, 'MAX_PDF_BYTES', 10 * 1024 * 1024)
        async def fail(*a): raise RuntimeError('offline')
        monkeypatch.setattr(media, 'put_bytes', fail)
        assert (await client.post(path + '/responses', params=params, headers=headers, content=pdf)).status_code == 503
        assert not (await quote_store.get(rec.id)).quotes
        rec.status = 'closed'
        await quote_store.save(rec)
        assert (await client.post(path + '/responses', params=params, headers=headers, content=pdf)).status_code == 409
        assert not uploaded


@pytest.mark.asyncio
async def test_changed_retry_cannot_replace_quote_and_homeowner_auth_still_applies(monkeypatch, pdf):
    rec, client, path, params, headers, _ = await setup(monkeypatch)
    async with client:
        assert (await client.post(path + '/responses', params=params, headers=headers, content=pdf)).status_code == 201
        changed = dict(params, provider_name='Another company')
        assert (await client.post(path + '/responses', params=changed, headers=headers, content=pdf)).status_code == 409
        current = await quote_store.get(rec.id)
        current.homeownerId = 'homeowner-private'
        await quote_store.save(current)
        assert (await client.get(f'/api/v1/ai/quote-requests/{rec.id}')).status_code == 403


@pytest.mark.asyncio
async def test_durable_receipt_failure_is_retryable_and_never_uses_local_success(monkeypatch, pdf):
    rec, client, path, params, headers, _ = await setup(monkeypatch)
    async def fail(*a): raise httpx.ConnectError('offline')
    class BrokenStore:
        post = staticmethod(fail)
    monkeypatch.setattr(provider_responses.supabase_store, '_rest', lambda: BrokenStore())
    quote = None
    async with client:
        assert (await client.post(path + '/responses', params=params, headers=headers, content=pdf)).status_code == 201
        quote = (await quote_store.get(rec.id)).quotes[0]
    monkeypatch.setattr(provider_responses.supabase_store, 'enabled', lambda: True)
    with pytest.raises(Exception) as exc:
        await provider_responses.save(rec.id, params['submission_id'], 'f' * 64, quote)
    assert exc.value.status_code == 503


def test_camera_pose_uses_arkit_column_major_negative_z_and_rejects_invalid():
    pose = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 4, 1.6, -2, 1]
    assert media.camera_pose({'cameraTransform': pose}) == {'position': [4, 1.6, -2], 'direction': [0, 0, -1]}
    pose[8:11] = [2, 0, 0]
    assert media.camera_pose({'cameraTransform': pose})['direction'] == [-1, 0, 0]
    pose[12] = float('nan')
    assert media.camera_pose({'cameraTransform': pose}) is None
    assert media.camera_pose({'cameraTransform': [1]}) is None


@pytest.mark.asyncio
async def test_status_exposes_scoped_camera_coordinates_not_storage_paths(monkeypatch):
    rec = await saved_snapshot(monkeypatch)
    rec.scanMedia['photos'][0].update(roomKey='room-1', camera={'position': [1, 2, 3], 'direction': [0, 0, -1]})
    await quote_store.save(rec)
    async def status(*a): return {'status': 'ready'}
    monkeypatch.setattr(media, 'conversion_status', status)
    exp = rec.scanMedia['expiresAt']
    path = f'/api/v1/quote-media/{rec.id}/{exp}/{media.signature(rec, exp)}/status'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        result = await client.get(path)
        assert result.json()['photos'][0]['camera']['position'] == [1, 2, 3]
        assert result.json()['models'][0]['key'] == 'home'
        assert 'private/' not in result.text
        assert 'quotes' not in result.json(), 'a provider cannot read other providers’ quotes'


@pytest.mark.asyncio
async def test_existing_email_backfills_only_its_frozen_context(monkeypatch, tmp_path):
    import zipfile
    import json
    snapshot = {'snapshotId': 'frozen', 'homeId': '25d800a5-d2ba-4ec0-9dd2-7509a7d28ec9',
                'ownerId': 'cdeca749-2170-4355-b92f-746e39429c61',
                'context': {'bucket': media.BUCKET, 'object': 'original-context.zip'},
                'rooms': [{'key': 'room-1', 'label': 'Kitchen', 'frameIds': ['frame-one']}],
                'photos': [{'frameId': 'frame-one', 'roomKey': 'room-1', 'label': 'Kitchen', 'object': 'old-photo.jpg'}]}
    pose = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 2, 1, -3, 1]
    image = io.BytesIO(); Image.new('RGB', (100, 100), 'red').save(image, 'JPEG')
    calls, cache = [], {}
    async def download(bucket, key, target, **kwargs):
        calls.append(key)
        with zipfile.ZipFile(target, 'w') as z:
            z.writestr('rooms/room-1/rebuild/manifest.json', json.dumps({'frames': [{'id': 'frame-one', 'cameraTransform': pose}]}))
            z.writestr('rooms/room-1/rebuild/images/frame-one.jpg', image.getvalue())
        return True
    async def read(key): return cache.get(key, {})
    async def put(key, data, content_type): cache[key] = json.loads(data)
    monkeypatch.setattr(media.supabase_store, 'enabled', lambda: True)
    monkeypatch.setattr(media.supabase_store, 'download_object', download)
    monkeypatch.setattr(media, 'get_json', read)
    monkeypatch.setattr(media, 'put_bytes', put)
    for _ in range(2):
        photos = await media.photos_with_camera(snapshot)
        assert photos[0]['camera']['position'] == [2, 1, -3]
        assert photos[0]['object'] == 'old-photo.jpg'
    assert calls == ['original-context.zip']
    assert 'camera' not in snapshot['photos'][0], 'the immutable snapshot was not changed'


@pytest.mark.asyncio
async def test_durable_rest_receipts_use_insert_only_and_reconcile_duplicates(monkeypatch):
    import json
    from app.flow_quotes import ReturnedQuote, QuoteDocument
    rows = {}
    def handle(request):
        assert request.url.path == '/flow_provider_responses'
        if request.method == 'POST':
            assert request.headers['prefer'] == 'resolution=ignore-duplicates'
            row = json.loads(request.content)
            rows.setdefault(row['id'], row)
            return httpx.Response(201, json=[])
        assert request.method == 'GET'
        rid = request.url.params['request_id'].removeprefix('eq.')
        return httpx.Response(200, json=[r for r in rows.values() if r['request_id'] == rid])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url='https://store.test') as client:
        monkeypatch.setattr(provider_responses.supabase_store, 'enabled', lambda: True)
        monkeypatch.setattr(provider_responses.supabase_store, '_rest', lambda: client)
        uid = str(uuid4())
        quote = ReturnedQuote(id=uid, providerName='One', document=QuoteDocument(fileName='one.pdf', byteCount=100))
        await provider_responses.save('qr1', uid, 'a' * 64, quote)
        await provider_responses.save('qr1', uid, 'a' * 64, quote)
        assert len(await provider_responses.responses('qr1')) == 1
        assert not await provider_responses.responses('qr2')
        with pytest.raises(Exception) as error:
            await provider_responses.save('qr1', uid, 'b' * 64, quote)
        assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_pdf_only_pricing_survives_retry_and_restart_without_inventing_amount(monkeypatch, pdf):
    rec, client, path, params, headers, uploaded = await setup(monkeypatch)
    async with client:
        missing = {k: v for k, v in params.items() if k != 'price_usd'}
        missing['earliest_available_date'] = '2026-11-02'  # Stale clients cannot restore the removed field.
        result = await client.post(path + '/responses', params=missing, headers=headers, content=pdf)
        assert result.status_code == 201, result.text
        assert (await client.post(path + '/responses', params=missing, headers=headers, content=pdf)).status_code == 200
        assert len(uploaded) == 1
        # A stale priced retry cannot silently replace an unpriced receipt.
        assert (await client.post(path + '/responses', params=params, headers=headers, content=pdf)).status_code == 409
        view = (await client.get(f'/api/v1/ai/quote-requests/{rec.id}')).json()
        assert view['quotes'][0].get('priceUsd') is None
        assert view['quotes'][0]['document']['fileName'] == 'estimate.pdf'
        assert 'earliestAvailableDate' not in view['quotes'][0]
    cold = await QuoteRequestStore().get(rec.id)
    assert len(cold.quotes) == 1
    assert cold.quotes[0].priceUsd is None
    assert cold.quotes[0].earliestAvailableDate is None


@pytest.mark.asyncio
async def test_larger_pdf_limit_accepts_over_10_mb_and_rejects_over_25(monkeypatch, pdf):
    _, client, path, params, headers, uploaded = await setup(monkeypatch)
    async with client:
        large = pdf[:-6] + b' ' * (11 * 1024 * 1024) + b'%%EOF'
        assert (await client.post(path + '/responses', params=params, headers=headers, content=large)).status_code == 201
        too_large = pdf[:-6] + b' ' * (25 * 1024 * 1024) + b'%%EOF'
        result = await client.post(path + '/responses', params=dict(params, submission_id=str(uuid4())), headers=headers, content=too_large)
        assert result.status_code == 413
        assert len(uploaded) == 1


def test_older_quotes_and_retry_fingerprints_remain_compatible():
    import hashlib
    from app.flow_quotes import ReturnedQuote
    quote = ReturnedQuote(providerName='Existing provider', priceUsd=100)
    assert quote.earliestAvailableDate is None
    assert 'earliestAvailableDate' not in quote.homeowner_view()
    assert provider_responses.fingerprint(b'pdf', 'Existing provider', None) == hashlib.sha256(
        b'pdf["Existing provider",null]').hexdigest()


@pytest.mark.asyncio
async def test_provider_email_is_persisted_and_cannot_change_on_pdf_retry(monkeypatch, pdf):
    rec, client, path, params, headers, uploaded = await setup(monkeypatch)
    params['provider_email'] = '  Quotes@BRIGHTLINE.EXAMPLE  '
    async with client:
        result = await client.post(path + '/responses', params=params, headers=headers, content=pdf)
        assert result.status_code == 201
        assert (await client.post(path + '/responses', params=params, headers=headers, content=pdf)).status_code == 200
        changed = dict(params, provider_email='someone-else@example.test')
        assert (await client.post(path + '/responses', params=changed, headers=headers, content=pdf)).status_code == 409
        view = await client.get(f'/api/v1/ai/quote-requests/{rec.id}')
        assert 'providerEmail' not in view.text
    cold = await QuoteRequestStore().get(rec.id)
    assert cold.quotes[0].providerEmail == 'Quotes@brightline.example'
    assert cold.quotes[0].acceptanceNotificationProviderEmail is None
    assert len(uploaded) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('email', ['', 'not-an-email', 'a@example.test,b@example.test',
    'Name <a@example.test>', 'a@example.test\r\nBcc: hidden@example.test', 'a..b@example.test'])
async def test_provider_email_rejects_invalid_or_multiple_recipients_before_upload(monkeypatch, pdf, email):
    _, client, path, params, headers, uploaded = await setup(monkeypatch)
    async with client:
        result = await client.post(path + '/responses', params=dict(params, provider_email=email), headers=headers, content=pdf)
        assert result.status_code == 422
    assert not uploaded


def test_ops_quote_correction_preserves_explicit_provider_mailbox_when_omitted(monkeypatch):
    from app.flow import partners
    from app.flow_api import _upsert_returned_quotes
    from app.flow_quotes import QuoteRequestRecord, ReturnedQuote
    monkeypatch.setattr(partners, 'note_quoted', lambda *args: None)
    record = QuoteRequestRecord(id='qr_mail', threadId='t', createdAt='2026-10-30', quotes=[
        ReturnedQuote(id='q1', providerName='Brightline', priceUsd=100, providerEmail='quotes@example.test')])
    _upsert_returned_quotes(record, [ReturnedQuote(providerName='Brightline', priceUsd=200)])
    assert record.quotes[0].id == 'q1'
    assert record.quotes[0].providerEmail == 'quotes@example.test'
