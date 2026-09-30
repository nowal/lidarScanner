"""Route/worker integration tests; SQL fencing is also tested on real Postgres
by tests/sql/scan_upload_transitions.sql, inside a rolled-back transaction.
"""
import copy
import json
import shutil
import zipfile

import pytest
from fastapi import BackgroundTasks, HTTPException
from app import flow_api
from app.config import settings
from app.flow import home_registry, scan_uploads, supabase_store
from app.home_index import HomeIndex, Room
from test_staged_scan_uploads import HOME, OWNER, REV, OTHER_REV, OBJECT, models, staged


def state(*, revision=REV, ready=True):
    index = HomeIndex([Room.from_json({'key': 'room-1', 'index': 1, 'name': 'kitchen'})],
        bundle_id=HOME, upload={'revision': revision, 'ownerId': OWNER, 'contextReady': True,
                               'modelsReady': False, 'durableV2': True})
    return {'revision': revision, 'ownerId': OWNER, 'objectPath': OBJECT,
            'progress': {'status': 'done', 'roomCount': 1}, 'leaseActive': False,
            'contextIndex': index.to_json() if ready else None, 'publishedIndex': None}


@pytest.mark.asyncio
async def test_worker_builds_private_context_without_replacing_published_models(staged, monkeypatch):
    old = HomeIndex([Room.from_json({'key': 'room-1', 'index': 1, 'name': 'old kitchen'})],
        bundle_id=HOME, home_model={'object': 'old-home'}, upload={'modelsReady': True})
    old.rename_room('room-1', 'my kitchen')
    old.rooms[0].model = {'object': 'old-room'}
    home_registry.save_index(HOME, old, durable=False)
    before = copy.deepcopy(old.to_json())
    archive = staged / 'context.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('meta.json', json.dumps({'id': HOME}))
        z.writestr('rooms/room-1/room.json', '{}')
        z.writestr('rooms/room-1/rebuild/manifest.json', '{"frames":[]}')
    async def download(bucket, path, target): shutil.copyfile(archive, target); return True
    completed = []
    async def transition(home, action, **kw):
        if action == 'read': return dict(state(ready=False), publishedIndex=before)
        if action == 'context_complete': completed.append(kw['payload']['index'])
        return {}
    def forbidden(*a, **kw): raise AssertionError('Pending ingestion must not touch shared room caches')
    monkeypatch.setattr(supabase_store, 'download_object', download)
    monkeypatch.setattr(scan_uploads, 'transition', transition)
    monkeypatch.setattr(scan_uploads.room_context, 'save', forbidden)
    await scan_uploads.ingest(HOME, 'metashape-exports', OBJECT, REV, OWNER, 'token', False)
    assert home_registry.load_index(HOME).to_json() == before
    assert len(completed) == 1
    assert completed[0]['homeModel']['object'] == 'old-home'
    assert completed[0]['rooms'][0]['name'] == 'my kitchen'
    assert completed[0]['upload']['contextReady'] and not completed[0]['upload']['modelsReady']


@pytest.mark.asyncio
async def test_late_model_verification_cannot_publish_over_new_revision(staged, monkeypatch):
    current = state()
    published = []
    async def transition(home, action, **kw):
        if action == 'read': return copy.deepcopy(current)
        if action == 'publish':
            assert current['revision'] == OTHER_REV
            raise HTTPException(409, detail={'code': 'revision_mismatch', 'retryable': False})
        published.append(action)
    async def size(*a): current['revision'] = OTHER_REV; return 64
    monkeypatch.setattr(scan_uploads, 'transition', transition)
    monkeypatch.setattr(supabase_store, 'stored_object_size', size)
    with pytest.raises(HTTPException) as error:
        await scan_uploads.publish(HOME, models(), OWNER)
    assert error.value.detail['code'] == 'revision_mismatch'
    assert not published


@pytest.mark.asyncio
async def test_only_lease_winner_schedules_work_and_retry_uses_saved_package(staged, monkeypatch):
    claim_count = 0
    async def transition(home, action, **kw):
        nonlocal claim_count
        result = state(ready=False)
        result['progress'] = {'status': 'running'}
        result['leaseActive'] = True
        if action == 'claim':
            claim_count += 1
            result['claimed'] = claim_count == 1
        return result
    monkeypatch.setattr(scan_uploads, 'transition', transition)
    one, two = BackgroundTasks(), BackgroundTasks()
    body = flow_api.ScanContextUpload(revision=REV, objectPath=OBJECT, generation=1)
    await scan_uploads.begin(HOME, body, OWNER, one)
    await scan_uploads.begin(HOME, body, OWNER, two)
    assert len(one.tasks) == 1 and not two.tasks
    assert one.tasks[0].args[2] == OBJECT


@pytest.mark.asyncio
async def test_fresh_database_index_wins_over_another_workers_stale_cache(staged, monkeypatch):
    home_registry.save_index(HOME, HomeIndex([], bundle_id=HOME), durable=False)
    async def transition(*a, **kw): return state()
    monkeypatch.setattr(scan_uploads, 'transition', transition)
    monkeypatch.setattr(supabase_store, 'enabled', lambda: True)
    index = await home_registry.load_index_async(HOME)
    assert len(index.rooms) == 1 and index.upload['revision'] == REV


def test_expired_lease_resubmits_and_old_object_is_never_ready():
    current = state(ready=False)
    current['progress'] = {'status': 'running'}
    assert scan_uploads.status(current, OBJECT)['status'] == 'unknown'
    assert scan_uploads.status(current, 'old.zip')['status'] == 'superseded'


@pytest.mark.asyncio
async def test_database_outage_is_retryable_and_never_uses_local_success(staged, monkeypatch):
    monkeypatch.setattr(supabase_store, 'enabled', lambda: True)
    class Broken:
        async def post(self, *a, **kw): raise RuntimeError('offline')
    monkeypatch.setattr(supabase_store, '_rest', lambda: Broken())
    with pytest.raises(HTTPException) as error:
        await scan_uploads.transition(HOME, 'read')
    assert error.value.status_code == 503 and error.value.detail['retryable']
