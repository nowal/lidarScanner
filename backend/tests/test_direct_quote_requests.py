"""Direct homeowner requests use real immutable scan receipts without any chat state."""
import asyncio
import copy
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from app import flow_api
from app.config import settings
from app.flow import home_registry, scan_uploads, supabase_store, ops_email
from app.flow_quotes import quote_store, QuoteRequestStore
from app.home_index import HomeIndex
from app.main import app
from test_quote_media import index, HOME, OWNER, REV


@pytest_asyncio.fixture
async def direct(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'storage_dir', str(tmp_path))
    monkeypatch.setattr(settings, 'flow_token_secret', 'direct-test')
    monkeypatch.setattr(settings, 'public_base_url', 'https://example.test')
    state = {'ownerId': OWNER, 'revision': REV, 'publishedIndex': index()}
    async def identity(token): return 'auth-owner' if token == 'owner' else None
    async def profile(sub): return {'id': OWNER}
    async def load(home): return HomeIndex.from_json(state['publishedIndex'])
    async def transition(*args, **kwargs): return state
    monkeypatch.setattr(flow_api, 'homeowner_id_from_header', identity)
    monkeypatch.setattr(supabase_store, 'resolve_homeowner', profile)
    monkeypatch.setattr(home_registry, 'load_index_async', load)
    monkeypatch.setattr(scan_uploads, 'transition', transition)
    scheduled = []
    monkeypatch.setattr(flow_api, '_schedule_ops_email', lambda r: scheduled.append(r.id))
    # Any accidental dependency on a chat state fails the test.
    async def no_chat(*args, **kwargs): raise AssertionError('Direct requests must not resolve chat')
    monkeypatch.setattr(flow_api, 'resolve_flow_state', no_chat)
    body = dict(submissionId=str(uuid4()), homeId=HOME, revision=REV,
                description='  Paint only the new walls.\nKeep the original trim & color.  ', zip='01234')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                 headers={'X-Homeowner-Token': 'owner'}) as client:
        yield client, body, state, scheduled


@pytest.mark.asyncio
async def test_direct_without_contact_or_chat_preserves_narrative_and_snapshot(direct):
    client, body, state, _ = direct
    response = await client.post('/api/v1/ai/quote-requests/direct', json=body)
    assert response.status_code == 201, response.text
    saved = await QuoteRequestStore().get(response.json()['quoteRequestId'])
    assert saved.synopsis == body['description'] and saved.zip == '01234'
    from app.flow.ops_email import build_ops_email_html
    import html
    rendered = build_ops_email_html(saved, [], [])
    assert html.escape(body['description']) in rendered
    assert 'Home project' in rendered
    assert saved.contactEmail is None and saved.address is None and saved.serviceType is None
    assert saved.homeownerId == OWNER and saved.scopeIntent == 'whole_home'
    assert saved.scopeOptions == saved.materials == [] and saved.roomKey is None
    assert saved.scanMedia['context']['object'] == index()['upload']['contextObject']
    assert saved.scanMedia['models'][0]['object'] == index()['homeModel']['object']
    assert [r['frameIds'] for r in saved.scanMedia['rooms']] == [['frame-1'], ['frame-2']]
    snapshot = copy.deepcopy(saved.scanMedia)
    state['publishedIndex']['homeModel']['object'] = 'later-model'
    assert (await QuoteRequestStore().get(saved.id)).scanMedia == snapshot
    status = await client.get('/api/v1/ai/quote-requests/' + saved.id)
    assert status.status_code == 200 and status.json()['quotes'] == []


@pytest.mark.asyncio
async def test_direct_retries_and_simultaneous_taps_restore_same_receipt(direct):
    client, body, state, scheduled = direct
    responses = await asyncio.gather(*[client.post('/api/v1/ai/quote-requests/direct', json=body) for _ in range(4)])
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 201]
    ids = {r.json()['quoteRequestId'] for r in responses}
    assert len(ids) == 1 and len(await quote_store.list()) == 1
    # An uncertain retry remains valid even if a later model is being uploaded.
    state['revision'] = str(uuid4())
    assert (await client.post('/api/v1/ai/quote-requests/direct', json=body)).status_code == 200
    body['description'] = 'Different work'
    assert (await client.post('/api/v1/ai/quote-requests/direct', json=body)).status_code == 409


@pytest.mark.asyncio
async def test_direct_explicit_scope_uses_only_selected_areas(direct):
    client, body, state, _ = direct
    state['publishedIndex']['rooms'][1].update(windows=1, windowOpenings=[[1.5, 2]],
        measurements={'paintable_sqft': 240, 'perimeter_m': 18})
    body['roomKeys'] = ['room-2']
    body['homeContext'] = {'totals': {'floorAreaSquareMeters': 9999}}
    response = await client.post('/api/v1/ai/quote-requests/direct', json=body)
    saved = await quote_store.get(response.json()['quoteRequestId'])
    assert saved.scopeIntent == 'selected_rooms'
    assert [r['key'] for r in saved.scanMedia['rooms']] == ['room-2']
    assert [m['key'] for m in saved.scanMedia['models']] == ['room-2']
    assert [r['name'] for r in saved.measurements['rooms']] == ['Living room']
    assert saved.measurements['paintableWallSquareFeet'] == 240
    assert saved.measurements['windowOpenings'] == [{'widthFeet': 4.9, 'heightFeet': 6.6}]
    assert saved.measurements['floorAreaSquareFeet'] == 0  # Never the unselected whole-home total.
    assert saved.measurements['rooms'][0]['geometry']['perimeter_m'] == 18


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [dict(description=' \n '), dict(zip='1234'), dict(zip=12345),
                                  dict(zip='123456'), dict(description='x'*10001), dict(roomKeys=['missing']),
                                  dict(homeContext={'scanId': str(uuid4())})])
async def test_direct_validation(direct, change):
    client, body, _, _ = direct
    response = await client.post('/api/v1/ai/quote-requests/direct', json=dict(body, **change))
    assert response.status_code == 422
    assert await quote_store.list() == []


@pytest.mark.asyncio
async def test_direct_pending_missing_models_and_ownership(direct):
    client, body, state, _ = direct
    assert (await client.post('/api/v1/ai/quote-requests/direct', json=body, headers={'X-Homeowner-Token': 'wrong'})).status_code == 401
    state['ownerId'] = str(uuid4())
    assert (await client.post('/api/v1/ai/quote-requests/direct', json=body)).status_code == 403
    state['ownerId'] = OWNER
    state['revision'] = str(uuid4())
    assert (await client.post('/api/v1/ai/quote-requests/direct', json=body)).status_code == 409
    state['revision'] = REV
    state['publishedIndex']['homeModel'] = {}
    assert (await client.post('/api/v1/ai/quote-requests/direct', json=body)).status_code == 409
    assert await quote_store.list() == []


@pytest.mark.asyncio
async def test_direct_durable_failure_keeps_retry_identity_and_no_email(direct, monkeypatch):
    client, body, _, scheduled = direct
    original = quote_store.create
    async def fail(*args): raise RuntimeError('offline')
    monkeypatch.setattr(quote_store, 'create', fail)
    assert (await client.post('/api/v1/ai/quote-requests/direct', json=body)).status_code == 503
    assert not scheduled and await quote_store.list() == []
    monkeypatch.setattr(quote_store, 'create', original)
    assert (await client.post('/api/v1/ai/quote-requests/direct', json=body)).status_code == 201
    assert len(await quote_store.list()) == 1


@pytest.mark.asyncio
async def test_create_only_rest_conflict_restores_winner_without_overwrite(direct, monkeypatch):
    client, body, _, _ = direct
    response = await client.post('/api/v1/ai/quote-requests/direct', json=body)
    original = await quote_store.get(response.json()['quoteRequestId'])
    losing = original.model_copy(deep=True)
    losing.synopsis = 'A competing process must not replace the saved narrative'
    calls = []
    class REST:
        async def post(self, path, **kwargs):
            calls.append(kwargs)
            return httpx.Response(201, json=[], request=httpx.Request('POST', 'https://example.test'))
    async def saved(*args): return original.model_dump(mode='json')
    monkeypatch.setattr(supabase_store, 'enabled', lambda: True)
    monkeypatch.setattr(supabase_store, '_rest', lambda: REST())
    monkeypatch.setattr(supabase_store, 'get_quote_request', saved)
    restored = await quote_store.create(losing)
    assert restored.synopsis == body['description']
    assert calls[0]['headers']['Prefer'] == 'resolution=ignore-duplicates,return=representation'


@pytest.mark.asyncio
@pytest.mark.parametrize('configured_cc', ['', 'Noah <noah@takeshapehome.com>, noah@takeshapehome.com'])
async def test_direct_email_reaches_both_ops_with_private_model_link_once(direct, monkeypatch, configured_cc):
    from urllib.parse import urlsplit
    from app.flow import quote_media
    from app.flow.ops_reply import allowed_reply_senders
    client, body, _, _ = direct
    monkeypatch.setattr(settings, 'ops_email', 'Quintin <quintin@takeshapehome.com>')
    monkeypatch.setattr(settings, 'ops_quote_cc', configured_cc)
    monkeypatch.setattr(settings, 'resend_api_key', 'isolated-transport')
    monkeypatch.setattr(quote_media, 'queue_conversion', lambda *args: None)
    response = await client.post('/api/v1/ai/quote-requests/direct', json=body)
    assert response.status_code == 201
    saved = await quote_store.get(response.json()['quoteRequestId'])
    # Photo extraction has separate archive tests; this fixture has no real ZIP.
    saved.scanMedia['photosPrepared'] = True
    await quote_store.save(saved)
    attempts = []
    async def transport(to, subject, text, html=None, **kwargs):
        attempts.append((to, subject, text, html, kwargs))
        if len(attempts) == 1:
            raise RuntimeError('Temporary transport failure')
    monkeypatch.setattr(ops_email, '_send_via_resend', transport)
    assert await ops_email.send_ops_email(saved) == 'failed'
    restored = await QuoteRequestStore().get(saved.id)
    assert await ops_email.send_ops_email(restored) == 'sent'
    assert attempts[0] == attempts[1]  # Same body and transport idempotency key.
    to, subject, text, html, options = attempts[-1]
    assert ops_email.recipient_list(to) == ['quintin@takeshapehome.com']
    assert options['cc'] == ['noah@takeshapehome.com']
    assert body['description'] in text and '01234' in subject
    link = quote_media.link(restored)
    assert link in text and link in html
    assert '/api/v1/quote-media/' in link
    assert (await client.get(urlsplit(link).path)).status_code == 200
    signed_objects = []
    async def sign(bucket, key, expires_in):
        signed_objects.append((bucket, key, expires_in))
        return 'https://storage.example/private-model'
    monkeypatch.setattr(supabase_store, '_signed_storage_url', sign)
    assert (await client.get(urlsplit(link).path + '/models/0/usdz')).status_code == 307
    assert signed_objects[-1][1] == index()['homeModel']['object']
    assert signed_objects[-1][2] <= 300
    assert 'noah@takeshapehome.com' not in allowed_reply_senders()
    assert (await client.post('/api/v1/ai/quote-requests/direct', json=body)).status_code == 200
    assert await ops_email.send_ops_email(saved) == 'already_sent'
    assert len(attempts) == 2
