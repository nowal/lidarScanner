import copy
import gzip
import hashlib
import io
import json
import zipfile

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException

from app import flow_api
from app.flow import model_archive as archive, scan_uploads, supabase_store
from app.flow import home_registry
from test_staged_scan_uploads import HOME, OWNER, REV, OTHER_REV, staged
from test_durable_scan_uploads import state


def package(tmp_path, *, extra=None):
    room = b'original embedded room model' * 30
    home = io.BytesIO()
    with zipfile.ZipFile(home, 'w', compression=zipfile.ZIP_STORED) as z:
        z.writestr('home.usda', '#usda 1.0\n')
        z.writestr('rooms/room-1/model.usdz', room)
        if extra: z.writestr(*extra)
    original = home.getvalue()
    zipped = gzip.compress(original, mtime=0)
    path = tmp_path / 'home.gz'
    path.write_bytes(zipped)
    model = dict(key='home', bucket='metashape-exports', encoding='gzip', bytes=len(zipped),
        objectPath=f'{OWNER}/{HOME}/home-{hashlib.sha256(zipped).hexdigest()}.usdz.gz',
        uncompressedBytes=len(original), sha256=hashlib.sha256(original).hexdigest())
    return path, model, original, room


def test_restores_exact_home_and_embedded_area_bytes_without_extracting_arbitrary_paths(tmp_path):
    path, model, home, room = package(tmp_path, extra=('../outside.usdz', 'never extract this'))
    files = archive.unpack(path, tmp_path, model, {'room-1'})
    assert files['home'].read_bytes() == home
    assert files['room-1'].read_bytes() == room
    assert not (tmp_path.parent / 'outside.usdz').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('token', ['valid', None])
async def test_preparation_and_status_require_verified_homeowner(staged, tmp_path, monkeypatch, token):
    _, model, _, _ = package(tmp_path)
    calls = []
    async def no_index(home): return None
    async def begin(home, body, owner, background):
        assert owner == OWNER
        calls.append('begin')
        return {'status': 'running', 'revision': REV}
    async def transition(home, action, revision, owner):
        assert owner == OWNER and revision == REV
        calls.append(action)
        return {'revision': REV, 'progress': {'status': 'done'}}
    monkeypatch.setattr(home_registry, 'load_index_async', no_index)
    monkeypatch.setattr(supabase_store, 'enabled', lambda: True)
    monkeypatch.setattr(archive, 'begin', begin)
    monkeypatch.setattr(archive, 'transition', transition)
    body = flow_api.CompressedHomeUpload(revision=REV, model=model)
    if token:
        result = await flow_api.upload_compressed_home(HOME, body, BackgroundTasks(), token)
        assert result.status_code == 202 and json.loads(result.body)['status'] == 'running'
        result = await flow_api.compressed_home_status(HOME, REV, token)
        assert json.loads(result.body)['status'] == 'done'
        assert calls == ['begin', 'read']
    else:
        with pytest.raises(HTTPException) as denied:
            await flow_api.upload_compressed_home(HOME, body, BackgroundTasks(), token)
        assert denied.value.status_code == 401
        with pytest.raises(HTTPException) as denied:
            await flow_api.compressed_home_status(HOME, REV, token)
        assert denied.value.status_code == 401 and not calls


@pytest.mark.parametrize('damage', ['sha', 'size', 'truncated', 'missing_area'])
def test_damaged_or_incomplete_archive_cannot_be_published(tmp_path, damage):
    path, model, _, _ = package(tmp_path)
    expected = {'room-1'}
    if damage == 'sha': model['sha256'] = '0' * 64
    if damage == 'size': model['uncompressedBytes'] -= 1
    if damage == 'truncated': path.write_bytes(path.read_bytes()[:-8])
    if damage == 'missing_area': expected.add('room-2')
    with pytest.raises(ValueError): archive.unpack(path, tmp_path, model, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_room', [False, True])
async def test_worker_only_publishes_after_all_exact_models_are_uploaded(staged, tmp_path, monkeypatch, fail_room):
    source, model, home, room = package(tmp_path)
    events = []
    uploaded = {}
    async def base_transition(*a, **kw): return state()
    async def transition(h, action, revision, owner, payload=None):
        events.append((action, copy.deepcopy(payload)))
        if action == 'complete':
            assert set(uploaded) == {'home', 'room-1'}
            assert uploaded['home'] == home and uploaded['room-1'] == room
            assert set(payload['models']) == {'home', 'room-1'}
            assert len(payload['assets']) == 2
        return {}
    async def download(bucket, path, target, **kw): target.write_bytes(source.read_bytes()); return True
    async def upload(file, object_path, url, save):
        key = file.stem
        if key == 'room-1' and fail_room: raise httpx.ConnectError('offline')
        await save('saved-session-' + key)
        uploaded[key] = file.read_bytes()
    monkeypatch.setattr(scan_uploads, 'transition', base_transition)
    monkeypatch.setattr(archive, 'transition', transition)
    monkeypatch.setattr(supabase_store, 'download_object', download)
    monkeypatch.setattr(archive, 'upload_model', upload)
    await archive.prepare(HOME, REV, OWNER, 'token', {'package': model, 'progress': {'status': 'running'}})
    assert any(action == 'complete' for action, _ in events) != fail_room
    if fail_room:
        failed = events[-1][1]['progress']
        assert failed['retryable'] and failed['uploads']['home'] == 'saved-session-home'


@pytest.mark.asyncio
async def test_only_lease_winner_runs_and_status_never_exposes_upload_sessions(tmp_path, monkeypatch):
    _, model, _, _ = package(tmp_path)
    body = flow_api.CompressedHomeUpload(revision=REV, model=model)
    claims = 0
    async def transition(home, action, revision, owner, payload=None):
        nonlocal claims
        if action == 'claim': claims += 1
        return dict(revision=REV, package=model, claimed=claims == 1, leaseActive=True,
                    progress={'status': 'running', 'uploads': {'home': 'private-url'}})
    monkeypatch.setattr(archive, 'transition', transition)
    a, b = BackgroundTasks(), BackgroundTasks()
    result = await archive.begin(HOME, body, OWNER, a)
    await archive.begin(HOME, body, OWNER, b)
    assert len(a.tasks) == 1 and not b.tasks
    assert 'uploads' not in result
    assert archive.status(dict(revision=REV, leaseActive=False, progress={'status': 'running'}))['status'] == 'unknown'


@pytest.mark.asyncio
async def test_foreign_archive_is_rejected_before_job_creation(tmp_path, monkeypatch):
    _, model, _, _ = package(tmp_path)
    model['objectPath'] = model['objectPath'].replace(OWNER, OTHER_REV)
    with pytest.raises(HTTPException) as error:
        await archive.begin(HOME, flow_api.CompressedHomeUpload(revision=REV, model=model), OWNER, BackgroundTasks())
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_backend_upload_resumes_saved_tus_offset(tmp_path, monkeypatch):
    file = tmp_path / 'home.usdz'
    original = b'0123456789'
    file.write_bytes(original)
    checks = 0
    async def size(*a):
        nonlocal checks
        checks += 1
        if checks == 1:
            response = httpx.Response(404, request=httpx.Request('HEAD', 'https://example.test'))
            raise httpx.HTTPStatusError('missing', request=response.request, response=response)
        return len(original)
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == 'HEAD': return httpx.Response(200, headers={'Upload-Offset': '4'})
        assert request.method == 'PATCH' and request.content == original[4:]
        assert request.headers['Upload-Offset'] == '4'
        return httpx.Response(204, headers={'Upload-Offset': '10'})
    async def save(url): pytest.fail('An existing TUS session must be reused')
    monkeypatch.setattr(supabase_store, 'stored_object_size', size)
    monkeypatch.setattr(archive, 'tus_endpoint', lambda: 'https://example.test/storage/v1/upload/resumable')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await archive.upload_model(file, 'owner/home/home-hash.usdz',
            'https://example.test/storage/v1/upload/resumable/session', save, client=client)
    assert [r.method for r in requests] == ['HEAD', 'PATCH']


@pytest.mark.asyncio
async def test_complete_object_is_adopted_after_lost_worker_response(tmp_path, monkeypatch):
    file = tmp_path / 'home.usdz'; file.write_bytes(b'already saved')
    async def size(*a): return file.stat().st_size
    async def save(url): pytest.fail('No new upload')
    monkeypatch.setattr(supabase_store, 'stored_object_size', size)
    await archive.upload_model(file, 'owner/home/home-hash.usdz', None, save)
