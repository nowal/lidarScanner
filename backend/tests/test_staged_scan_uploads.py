"""The phone can finish texturing after Home Guide is already using the home."""
import json
import shutil
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException
from app import flow_api, room_context
from app.config import settings
from app.flow import home_registry, supabase_store
from app.home_index import HomeIndex, Room
from app.flow.state import FlowState
from app.flow_quotes import build_model_link

HOME = '11111111-1111-1111-1111-111111111111'
OWNER = '22222222-2222-2222-2222-222222222222'
REV = '33333333-3333-3333-3333-333333333333'
OTHER_REV = '44444444-4444-4444-4444-444444444444'
OBJECT = f'{OWNER}/{HOME}/context.zip'
IDENTITY = [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]


@pytest.fixture
def staged(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'storage_dir', str(tmp_path / 'storage'))
    home_registry._cache.clear()
    home_registry._ingest_status.clear()
    async def identity(token): return 'auth-user' if token == 'valid' else None
    async def homeowner(sub): return {'id': OWNER}
    async def owners(home): return {OWNER}
    async def size(*a): return 64
    monkeypatch.setattr(flow_api, 'homeowner_id_from_header', identity)
    monkeypatch.setattr(supabase_store, 'resolve_homeowner', homeowner)
    monkeypatch.setattr(supabase_store, 'scan_upload_owners', owners)
    monkeypatch.setattr(supabase_store, 'stored_object_size', size)
    yield tmp_path
    home_registry._cache.clear()
    home_registry._ingest_status.clear()


def seed(*, ready=False):
    index = HomeIndex([Room.from_json({'key': 'room-1', 'index': 1, 'storey': 1, 'name': 'my kitchen'})], bundle_id=HOME,
                      upload={'ownerId': OWNER, 'revision': REV, 'contextObject': OBJECT,
                              'contextReady': True, 'modelsReady': ready})
    index.rooms[0].appearance = {'materials': ['wood']}
    index.rooms[0].measurements = {'floorAreaSquareFeet': 180}
    index.rename_room('room-1', 'my kitchen')
    home_registry.save_index(HOME, index)
    return index


def models(revision=REV):
    return flow_api.ScanModelsUpload(revision=revision, models=[
        flow_api.ScanModelUpload(key=k, objectPath=f'{OWNER}/{HOME}/{k}.usdz', bytes=64)
        for k in ['room-1', 'home']])


@pytest.mark.asyncio
async def test_late_models_keep_room_context_and_survive_cold_load(staged, monkeypatch):
    seed()
    result = await flow_api.upload_scan_models(HOME, models(), 'valid')
    assert json.loads(result.body)['status'] == 'done'
    home_registry._cache.clear()
    index = home_registry.load_index(HOME)
    assert index.upload['modelsReady'] is True
    assert index.rooms[0].display_name == 'my kitchen'
    assert index.rooms[0].named_by_homeowner
    assert index.rooms[0].appearance == {'materials': ['wood']}
    assert index.rooms[0].measurements['floorAreaSquareFeet'] == 180
    calls = []
    async def sign(bucket, path):
        calls.append((bucket, path)); return 'https://signed.example/model'
    monkeypatch.setattr(supabase_store, '_signed_storage_url', sign)
    state = FlowState(thread_id='t', home_id=HOME, active_room_key='room-1')
    link = await build_model_link(state)
    assert link['url'] == 'https://signed.example/model'
    assert calls == [('metashape-exports', f'{OWNER}/{HOME}/room-1.usdz')]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['session', 'foreign_owner', 'foreign_path', 'bucket', 'traversal', 'partial', 'stale', 'missing_room'])
async def test_models_cannot_be_registered_incorrectly(staged, kind):
    index = seed()
    body, token = models(), 'valid'
    if kind == 'session': token = None
    if kind == 'foreign_owner': index.upload['ownerId'] = 'someone-else'
    if kind == 'foreign_path': body.models[0].objectPath = f'other/{HOME}/room-1.usdz'
    if kind == 'bucket': body.models[0].bucket = 'home-assets'
    if kind == 'traversal': body.models[0].objectPath = f'{OWNER}/{HOME}/../room-1.usdz'
    if kind == 'partial': body.models[0].bytes = 65
    if kind == 'stale': body.revision = OTHER_REV
    if kind == 'missing_room': body.models.pop(0)
    with pytest.raises(HTTPException):
        await flow_api.upload_scan_models(HOME, body, token)
    assert home_registry.load_index(HOME).upload['modelsReady'] is False


@pytest.mark.asyncio
async def test_registration_failure_does_not_claim_models_ready(staged, monkeypatch):
    seed()
    async def fail(*a): raise RuntimeError('durable write failed')
    monkeypatch.setattr(home_registry, 'save_index_confirmed', fail)
    with pytest.raises(HTTPException) as error:
        await flow_api.upload_scan_models(HOME, models(), 'valid')
    assert error.value.status_code == 503
    assert home_registry.load_index(HOME).upload['modelsReady'] is False


@pytest.mark.asyncio
async def test_context_reservation_does_not_silently_drop_a_new_upload(staged):
    body = flow_api.ScanContextUpload(objectPath=OBJECT, revision=REV)
    background = BackgroundTasks()
    await flow_api.upload_scan_context(HOME, body, background, 'valid')
    assert len(background.tasks) == 1
    duplicate = BackgroundTasks()
    await flow_api.upload_scan_context(HOME, body, duplicate, 'valid')
    assert len(duplicate.tasks) == 0
    with pytest.raises(HTTPException) as error:
        await flow_api.upload_scan_context(HOME, body.model_copy(update={'objectPath': OBJECT.replace('context.zip', 'new.zip')}), BackgroundTasks(), 'valid')
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_status_works_after_server_restart_and_blocks_other_guests(staged):
    seed()
    home_registry._cache.clear()
    response = await flow_api.scan_context_status(HOME, OBJECT, 'valid')
    assert json.loads(response.body) == {'status': 'done', 'roomCount': 1}
    with pytest.raises(HTTPException):
        await flow_api.scan_context_status(HOME, OBJECT, 'bad')


@pytest.mark.asyncio
async def test_small_context_ingests_without_a_model_and_failure_is_visible(staged, monkeypatch):
    archive = staged / 'context.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('meta.json', json.dumps({'id': HOME}))
        z.writestr('rooms/room-1/room.json', '{}')
        z.writestr('rooms/room-1/floor.json', '{"floor": 1}')
        z.writestr('rooms/room-1/rebuild/manifest.json', '{"frames": []}')
    async def download(bucket, path, target): shutil.copyfile(archive, target); return True
    monkeypatch.setattr(supabase_store, 'download_object', download)
    status = await home_registry.ingest_from_storage(HOME, 'metashape-exports', OBJECT,
                                                    enrich=False, upload_revision=REV, owner_id=OWNER)
    assert status['status'] == 'done' and status['roomCount'] == 1
    assert home_registry.load_index(HOME).upload['modelsReady'] is False
    assert home_registry.load_index(HOME).upload['contextReady'] is True
    async def fail(*a): raise RuntimeError('durable write failed')
    monkeypatch.setattr(home_registry, 'save_index_confirmed', fail)
    new_object = OBJECT.replace('context.zip', 'new.zip')
    status = await home_registry.ingest_from_storage(HOME, 'metashape-exports', new_object,
                                                    enrich=False, upload_revision=OTHER_REV, owner_id=OWNER)
    assert status['status'] == 'failed'
    response = await flow_api.scan_context_status(HOME, new_object, 'valid')
    assert json.loads(response.body)['status'] == 'failed'


@pytest.mark.asyncio
async def test_zero_room_upload_fails_without_overwriting_home(staged, monkeypatch):
    original = seed(ready=True).to_json()
    archive = staged / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as z: z.writestr('meta.json', '{}')
    async def download(bucket, path, target): shutil.copyfile(archive, target); return True
    monkeypatch.setattr(supabase_store, 'download_object', download)
    status = await home_registry.ingest_from_storage(HOME, 'metashape-exports', OBJECT)
    assert status['status'] == 'failed'
    assert home_registry.load_index(HOME).to_json() == original


def test_preselected_photos_are_not_selected_or_suppressed_again():
    frames = [{'id': str(i), 'cameraTransform': IDENTITY, 'intrinsics': [1]*9} for i in range(4)]
    manifest = {'frames': frames, 'aiSelectionVersion': 1, 'aiSelectedFrameIds': ['3', '1', '2', '0']}
    selected = room_context.select_context_frames({}, manifest)
    assert [f['id'] for f in selected] == ['3', '1', '2', '0']
    assert all(f['turns'] == 0 for f in selected)


@pytest.mark.asyncio
async def test_forgetting_a_staged_home_removes_its_direct_uploads(staged, monkeypatch):
    import asyncio
    seed(ready=True)
    calls = []
    async def remove_scan(home, owner): calls.append((home, owner)); return True
    async def remove(*a): return True
    monkeypatch.setattr(supabase_store, 'enabled', lambda: True)
    monkeypatch.setattr(supabase_store, 'delete_scan_uploads', remove_scan)
    monkeypatch.setattr(supabase_store, 'delete_home_index', remove)
    monkeypatch.setattr(supabase_store, 'delete_home_models', remove)
    home_registry.forget(HOME)
    await asyncio.gather(*list(home_registry._pending))
    assert calls == [(HOME, OWNER)]
    assert home_registry.load_index(HOME) is None
