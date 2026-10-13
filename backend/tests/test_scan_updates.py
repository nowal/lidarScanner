"""Assistant-only event protocol. All generation and storage are injected."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from app import flow_api, flow_runtime, home_ai, scan_update_provider
from app.armor import turn_guard
from app.config import settings
from app.flow import scan_updates, supabase_store
from app.flow.scan_update_context import changes, copy_parts, render, source_fingerprint
from app.flow.state import FlowState, QuoteRequestRef, FlowStep
from app.home_index import HomeIndex, Room

HOME, OWNER, REV = (str(uuid4()) for _ in range(3))


def room(key='room-1', fingerprint='one', x=0, **kw):
    return {'key': key, 'name': 'kitchen', 'confident': True, 'contextFingerprint': fingerprint,
            'polygon': [[x, 0], [x+4, 0], [x+4, 4], [x, 4]], 'floorY': 0,
            'appearance': {'surfaces': {'walls': 'pale blue', 'floor': 'wood'}}, **kw}


def request(**kw):
    return scan_updates.ScanUpdateRequest(threadId='thread', homeId=HOME, revision=REV, **kw)


class Receipts:
    """Small injected durable store; SQL implementation is tested separately."""
    def __init__(self):
        self.row = {'status': 'pending', 'eventId': str(uuid4()), 'message': None,
                    'baselineIndex': {'rooms': [room()]},
                    'contextIndex': {'rooms': [room(), room('room-2', 'two', 10)]}}
        self.token = None
        self.calls = []
        self.revision = REV
        self.fail_saves = 0
        self.save_then_disconnect = False

    async def transition(self, body, owner, action, payload):
        self.calls.append(action)
        if str(body.revision) != self.revision:
            raise HTTPException(409, detail={'code': 'revision_mismatch'})
        claimed = False
        if action == 'claim' and self.row['status'] == 'pending' and self.token is None:
            self.token, claimed = payload['token'], True
        elif action == 'finish':
            if self.fail_saves:
                self.fail_saves -= 1
                raise HTTPException(503)
            self.row.update(status='ready', message=payload['message'])
            if self.save_then_disconnect:
                self.save_then_disconnect = False
                raise HTTPException(503)
        elif action == 'fail':
            self.token = None
        elif action == 'skip':
            self.row['status'] = 'skipped'
        elif action == 'delivered':
            assert payload['eventId'] == self.row['eventId']
            self.row['deliveredAt'] = 'now'
        return copy.deepcopy(dict(self.row, claimed=claimed))


@pytest.fixture
def event(monkeypatch):
    store = Receipts()
    monkeypatch.setattr(scan_updates, 'transition', store.transition)
    generated = AsyncMock(return_value=['room-2:walls'])
    monkeypatch.setattr(scan_updates, 'select_observations', generated)
    # Any entry into chat/flow persistence is a regression.
    def forbidden(*a, **kw): raise AssertionError('Event entered the homeowner turn path')
    monkeypatch.setattr(home_ai, 'generate_home_ai_response', forbidden)
    monkeypatch.setattr(flow_runtime, 'persist_flow_state', forbidden)
    return store, generated


def test_added_refreshed_same_count_and_unchanged_models():
    a = room()
    assert not changes({'rooms': [a]}, {'rooms': [dict(a, model={'object': 'rebuilt'}, name='new name')]})
    b = room('room-8', 'new', 10)
    assert changes({'rooms': [a]}, {'rooms': [b]})[0]['kind'] == 'added'  # same count!
    assert changes({'rooms': [a]}, {'rooms': [room(fingerprint='new')]})[0]['kind'] == 'refreshed'
    assert not changes({'rooms': [a]}, {'rooms': [a]})  # first opener baseline


def test_new_capture_key_in_same_area_preserves_homeowner_name():
    before = {'rooms': [room(name='Mum’s studio', namedByHomeowner=True)]}
    delta = changes(before, {'rooms': [room('room-8', 'new')]})
    assert delta[0]['kind'] == 'refreshed' and delta[0]['matchedKey'] == 'room-1'
    assert delta[0]['label'] == 'Mum’s studio'


def test_unknown_or_ambiguous_area_never_becomes_a_confident_new_room():
    after = room('room-9', 'new', confident=False, polygon=[])
    delta = changes({'rooms': [room()]}, {'rooms': [after]})
    assert delta[0]['kind'] == 'additional_views'
    intro, facts = copy_parts(delta)
    result = render(intro, facts, ['invented:brick', 'room-9:walls'])
    assert 'can’t confidently identify' in result
    assert 'pale blue' in result and 'brick' not in result and '?' not in result
    assert changes({'rooms': [room(), room('room-2')]}, {'rooms': [room('room-3')]})[0]['kind'] == 'additional_views'


def test_geometry_only_never_describes_materials():
    delta = changes({'rooms': []}, {'rooms': [room(appearance={'coverage': 'geometry_only', 'surfaces': {'walls': 'brick'}})]})
    intro, facts = copy_parts(delta)
    text = render(intro, facts, ['room-1:walls'])
    assert 'brick' not in text and 'not enough clear photo detail' in text


def test_fingerprint_ignores_models_and_json_key_order_but_tracks_photo_bytes(tmp_path):
    root = tmp_path / 'rooms/room-1'
    (root / 'rebuild/images').mkdir(parents=True)
    (root / 'room.json').write_text('{"walls":[],"objects":[]}')
    photo = root / 'rebuild/images/a.jpg'
    photo.write_bytes(b'one')
    original = source_fingerprint(tmp_path, 'room-1')
    (root / 'room.json').write_text('{"objects":[], "walls": []}')
    (root / 'model.usdz').write_bytes(b'model rebuilt')
    assert original == source_fingerprint(tmp_path, 'room-1')
    photo.write_bytes(b'two')
    assert original != source_fingerprint(tmp_path, 'room-1')
    saved = HomeIndex([Room.from_json(room())])
    assert HomeIndex.from_json(saved.to_json()).rooms[0].context_fingerprint == 'one'


@pytest.mark.asyncio
async def test_first_scan_and_model_only_no_generation(event):
    store, generated = event
    store.row['contextIndex'] = copy.deepcopy(store.row['baselineIndex'])
    result = await scan_updates.run(request(), OWNER)
    assert result['status'] == 'none' and not result['delivered']
    generated.assert_not_awaited()


@pytest.mark.asyncio
async def test_retries_restart_and_concurrent_workers_reuse_saved_reply(event, monkeypatch):
    store, generated = event
    # Prove the durable lease is sufficient even when process locks are off.
    monkeypatch.setattr(settings, 'turn_lock_enabled', False)
    async def generate(facts):
        await asyncio.sleep(.02)
        return ['room-2:walls']
    generated.side_effect = generate
    one, two = await asyncio.gather(scan_updates.run(request(), OWNER), scan_updates.run(request(), OWNER))
    assert {one['status'], two['status']} == {'pending', 'ready'}
    again = await scan_updates.run(request(), OWNER)
    assert again['message'] == one['message'] and not again['delivered']
    assert generated.await_count == 1
    acknowledged = await scan_updates.run(request(deliveredEventId=one['eventId']), OWNER)
    assert acknowledged['delivered']
    # A lost ACK response or a reinstalled client can still recover the text.
    assert (await scan_updates.run(request(), OWNER))['message'] == one['message']
    assert generated.await_count == 1


@pytest.mark.asyncio
async def test_generation_failure_is_pending_and_recovers(event):
    store, generated = event
    generated.side_effect = RuntimeError('provider offline')
    with pytest.raises(HTTPException) as exc:
        await scan_updates.run(request(), OWNER)
    assert exc.value.status_code == 503 and store.row['status'] == 'pending'
    generated.side_effect = None
    assert (await scan_updates.run(request(), OWNER))['status'] == 'ready'


@pytest.mark.asyncio
@pytest.mark.parametrize('ambiguous', [False, True])
async def test_save_retry_does_not_repeat_generation(event, ambiguous):
    store, generated = event
    store.fail_saves = 0 if ambiguous else 2
    store.save_then_disconnect = ambiguous
    result = await scan_updates.run(request(), OWNER)
    assert result['status'] == 'ready' and not result['delivered']
    assert generated.await_count == 1


@pytest.mark.asyncio
async def test_persistent_write_failure_remains_recoverable(event):
    store, generated = event
    store.fail_saves = 3
    with pytest.raises(HTTPException): await scan_updates.run(request(), OWNER)
    assert store.row['status'] == 'pending' and 'deliveredAt' not in store.row
    assert (await scan_updates.run(request(), OWNER))['status'] == 'ready'


@pytest.mark.asyncio
async def test_superseding_revision_during_generation_rejects_reply(event):
    store, generated = event
    async def supersede(facts):
        store.revision = str(uuid4())
        return []
    generated.side_effect = supersede
    with pytest.raises(HTTPException) as exc: await scan_updates.run(request(), OWNER)
    assert exc.value.detail['code'] == 'revision_mismatch'
    assert store.row['message'] is None


@pytest.mark.asyncio
async def test_event_waits_for_ordinary_turn_and_leaves_every_state_field_unchanged(event, monkeypatch):
    store, generated = event
    monkeypatch.setattr(settings, 'turn_lock_enabled', True)
    state = FlowState(thread_id='thread', home_id=HOME, active_room_key='room-1',
                      homeowner_id=OWNER, step=FlowStep.QUOTE_REQUEST,
                      quote_request=QuoteRequestRef(id='existing', presented_quote_count=2))
    state.slots.first_name, state.slots.address = 'Jo', '123 Sample Road'
    before = state.model_dump()
    async with turn_guard('thread'):
        task = asyncio.create_task(scan_updates.run(request(), OWNER))
        await asyncio.sleep(.01)
        assert not generated.await_count and not task.done()
    assert (await task)['status'] == 'ready'
    assert state.model_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('code,status', [('owner_mismatch',403), ('thread_home_mismatch',403),
    ('revision_mismatch',409), ('context_not_ready',409), ('thread_not_ready',409)])
async def test_storage_errors_fail_closed_and_never_use_local_state(monkeypatch, code, status):
    monkeypatch.setattr(supabase_store, 'enabled', lambda: True)
    class Rest:
        async def post(self, *a, **kw):
            return httpx.Response(200, json={'error': code}, request=httpx.Request('POST', 'https://test/rpc'))
    monkeypatch.setattr(supabase_store, '_rest', Rest)
    with pytest.raises(HTTPException) as exc: await scan_updates.transition(request(), OWNER, 'claim', {})
    assert exc.value.status_code == status


@pytest.mark.asyncio
async def test_network_outage_no_fallback_success(monkeypatch):
    monkeypatch.setattr(supabase_store, 'enabled', lambda: True)
    class Rest:
        async def post(self, *a, **kw): raise RuntimeError('offline')
    monkeypatch.setattr(supabase_store, '_rest', Rest)
    with pytest.raises(HTTPException) as exc: await scan_updates.transition(request(), OWNER, 'claim', {})
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_route_uses_verified_scan_owner_for_guest_and_account(monkeypatch):
    owner = AsyncMock(return_value=OWNER)
    run = AsyncMock(return_value={'status': 'none'})
    monkeypatch.setattr(flow_api, '_scan_upload_owner', owner)
    monkeypatch.setattr(scan_updates, 'run', run)
    for token in ('guest-token', 'upgraded-account-token'):
        await flow_api.home_ai_scan_update(request(), token)
        owner.assert_awaited_with(HOME, token)
        run.assert_awaited_with(request(), OWNER)
    owner.side_effect = HTTPException(403)
    with pytest.raises(HTTPException): await flow_api.home_ai_scan_update(request(), 'foreign')
    assert run.await_count == 2


@pytest.mark.asyncio
async def test_event_provider_is_stateless_and_uses_only_evidence_selection(monkeypatch):
    from types import SimpleNamespace
    from app import anthropic_provider
    monkeypatch.setattr(settings, 'ai_provider', 'anthropic')
    monkeypatch.setattr(settings, 'anthropic_api_key', 'injected-not-a-credential')
    calls = []
    class Client:
        messages = None
        def __init__(self): self.messages = self
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def create(self, **kw):
            calls.append(kw)
            return SimpleNamespace(stop_reason='end_turn', content=[SimpleNamespace(type='text', text='{"observationIds":["a"]}')])
    monkeypatch.setattr(anthropic_provider, '_client', Client)
    result = await scan_update_provider.select_observations([{'id':'a','text':'Pale walls'}, {'id':'b','text':'Wood floor'}])
    assert result == ['a']
    assert set(calls[0]['output_config']['format']['schema']['properties']) == {'observationIds'}
    assert 'flowCapture' not in json.dumps(calls) and 'previous_response_id' not in json.dumps(calls)


@pytest.mark.asyncio
async def test_openai_event_does_not_reuse_or_save_provider_continuation(monkeypatch):
    monkeypatch.setattr(settings, 'ai_provider', 'openai')
    monkeypatch.setattr(settings, 'openai_api_key', 'injected-not-a-credential')
    calls = []
    def forbidden(*a, **kw): raise AssertionError('Touched ordinary provider continuation')
    monkeypatch.setattr(home_ai, '_load_openai_thread_state', forbidden)
    class Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kw):
            calls.append(kw['json'])
            return httpx.Response(200, request=httpx.Request('POST', url), json={
                'output': [{'type':'message','content':[{'type':'output_text','text':'{"observationIds":["a"]}'}]}]})
    monkeypatch.setattr(scan_update_provider.httpx, 'AsyncClient', Client)
    assert await scan_update_provider.select_observations([{'id':'a','text':'Pale walls'}, {'id':'b','text':'Wood floor'}]) == ['a']
    assert calls[0]['store'] is False and 'previous_response_id' not in calls[0]


def test_legacy_indices_compare_evidence_not_new_fingerprint_presence_or_generated_prose():
    old = room()
    del old['contextFingerprint']
    refreshed = dict(old, contextFingerprint='now-hashed', appearance={'surfaces': {'walls': 'rephrased'}})
    assert not changes({'rooms': [old]}, {'rooms': [refreshed]})
    refreshed['frameIds'] = ['new-photo']
    assert changes({'rooms': [old]}, {'rooms': [refreshed]})[0]['kind'] == 'refreshed'


@pytest.mark.asyncio
async def test_real_opening_keeps_its_message_and_zero_user_turns_while_recording_baseline(tmp_path, monkeypatch):
    from app.flow import home_registry
    from test_flow_api import _stub_opening_generate, sample_context
    monkeypatch.setattr(settings, 'storage_dir', str(tmp_path))
    generate = AsyncMock(side_effect=_stub_opening_generate())
    monkeypatch.setattr(flow_runtime, 'generate_home_ai_response', generate)
    index = HomeIndex([Room.from_json(room())], bundle_id=HOME,
                     upload={'revision': REV, 'contextReady': True, 'modelsReady': False})
    home_registry.save_index(HOME, index, durable=False)
    opening = flow_runtime.HomeAIOpeningRequest(threadId='opening-baseline', homeId=HOME, homeContext=sample_context())
    first = await flow_runtime.run_opening_turn(opening)
    state = await flow_runtime.resolve_flow_state(first.threadId, first.flow.token)
    assert state.opening_scan_revision == REV and state.user_turns == 0 and state.quote_request is None
    index.upload['revision'] = str(uuid4())
    home_registry.save_index(HOME, index, durable=False)
    repeated = await flow_runtime.run_opening_turn(opening)
    assert repeated.message == first.message and generate.await_count == 1


@pytest.mark.asyncio
async def test_http_route_enforces_service_session_and_scan_owner_before_generation(monkeypatch):
    from fastapi import FastAPI
    from app.flow import home_registry
    app = FastAPI()
    app.include_router(flow_api.router)
    monkeypatch.setattr(settings, 'auth_token', 'test-service')
    async def verify(token): return 'guest-sub' if token == 'verified' else None
    monkeypatch.setattr(flow_api, 'homeowner_id_from_header', verify)
    monkeypatch.setattr(supabase_store, 'resolve_homeowner', AsyncMock(return_value={'id': OWNER}))
    index = HomeIndex([], upload={'ownerId': str(uuid4())})
    monkeypatch.setattr(home_registry, 'load_index_async', AsyncMock(return_value=index))
    run = AsyncMock(return_value={'status': 'none'})
    monkeypatch.setattr(scan_updates, 'run', run)
    body = request().model_dump(mode='json')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        path = '/api/v1/ai/home-chat/scan-update'
        assert (await client.post(path, json=body)).status_code == 401
        headers = {'Authorization': 'Bearer test-service'}
        assert (await client.post(path, json=body, headers=headers)).status_code == 401
        headers['X-Homeowner-Token'] = 'verified'
        assert (await client.post(path, json=body, headers=headers)).status_code == 403
        assert run.await_count == 0
        index.upload['ownerId'] = OWNER
        assert (await client.post(path, json=body, headers=headers)).status_code == 200
        run.assert_awaited_once_with(request(), OWNER)
