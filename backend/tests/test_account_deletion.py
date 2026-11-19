import json
import sqlite3
from uuid import uuid4

import httpx
import pytest

from app import account_deletion as deletion
from app.config import settings
from app.flow import home_registry, supabase_store
from app.main import app


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'supabase_url', 'https://supabase.invalid')
    monkeypatch.setattr(settings, 'supabase_service_role_key', 'server-secret')
    monkeypatch.setattr(settings, 'storage_dir', str(tmp_path))
    monkeypatch.setattr(settings, 'auth_token', 'app-token')
    return str(uuid4())


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer app-token'},
    {'Authorization': 'Bearer wrong', 'X-Homeowner-Token': 'stolen-user-id'}])
async def test_requires_both_app_and_verified_account_headers(service, headers, monkeypatch):
    async def forbidden(*args): raise AssertionError('Must not mutate')
    monkeypatch.setattr(deletion, 'delete_account', forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.delete('/api/v1/account', headers=headers)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_account_id_comes_from_auth_server_not_claims_or_body(service, monkeypatch):
    actual_client = httpx.AsyncClient
    deleted = []
    def auth(request):
        assert request.url.path == '/auth/v1/user'
        assert request.headers['Authorization'] == 'Bearer signed-token'
        return httpx.Response(200, json={'id': service, 'user_metadata': {'user_id': str(uuid4())}})
    async def remove(user, token, client): deleted.append((user, token))
    monkeypatch.setattr(deletion, 'delete_account', remove)
    monkeypatch.setattr(deletion.httpx, 'AsyncClient', lambda **kwargs: actual_client(transport=httpx.MockTransport(auth), **kwargs))
    async with actual_client(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.request('DELETE', '/api/v1/account',
            headers={'Authorization': 'Bearer app-token', 'X-Homeowner-Token': 'signed-token'},
            json={'user_id': str(uuid4())})
    assert response.status_code == 204
    assert deleted == [(service, 'signed-token')]


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [401,403,404])
async def test_rejects_invalid_or_deleted_auth_user(service, code):
    async with httpx.AsyncClient(base_url='https://supabase.invalid', transport=httpx.MockTransport(
        lambda request: httpx.Response(code, json={}))) as client:
        with pytest.raises(deletion.HTTPException) as exc:
            await deletion._verified_user('invalid', client)
    assert exc.value.status_code == 401


def manifest(owner='owner'):
    return dict(owners=[owner], providers=[], homes=['home'], requests=['request'], threads=['thread'], quoteIds=[], objects=[])


@pytest.mark.asyncio
async def test_storage_first_rechecks_batches_removes_local_copies_and_hard_deletes(service, monkeypatch, tmp_path):
    calls = []
    objects = [{'bucket':'metashape-exports','path':'owner/home/video.mov'},
               {'bucket':'home-assets','path':'home-indexes/home.json'}]
    async def rpc(name, user):
        assert user == service
        calls.append(name)
        return {**manifest(), 'objects': objects.copy()}
    def network(request):
        calls.append(request.url.path)
        if request.url.path.startswith('/storage/'):
            assert request.headers['Authorization'] == 'Bearer server-secret'
            paths=json.loads(request.content)['prefixes']
            objects[:] = [obj for obj in objects if obj['path'] not in paths]
        if request.url.path.startswith('/auth/v1/admin'):
            assert json.loads(request.content) == {'should_soft_delete':False}
        return httpx.Response(200, json={})
    monkeypatch.setattr(deletion, '_rpc', rpc)
    for file in ['homes/home.json','room_context/home/room-1.json','quote_requests/request.json',
                 'provider_responses/request/response.json','flow_state/thread.json',
                 'flow_state/thread.opening.json','flow_journal/thread.jsonl']:
        p=tmp_path/file; p.parent.mkdir(parents=True,exist_ok=True); p.write_text('{}')
    journal=tmp_path/'flow_journal/journal.jsonl'
    journal.write_text(json.dumps({'homeownerId':'owner'})+'\n'+json.dumps({'homeownerId':'someone-else'})+'\n')
    home_registry._cache['home']=object()
    supabase_store._homeowner_cache[service]=(0,{})
    async with httpx.AsyncClient(base_url='https://supabase.invalid', headers=deletion._headers(),
                                 transport=httpx.MockTransport(network)) as client:
        monkeypatch.setattr(supabase_store, '_rest', lambda: client)
        await deletion.delete_account(service,'signed-token',client)
    assert not objects
    assert calls.index('account_deletion_remove_profiles') > calls.index('/storage/v1/object/home-assets')
    assert calls.index('/auth/v1/logout') < calls.index('/auth/v1/admin/users/'+service)
    assert 'home' not in home_registry._cache and service not in supabase_store._homeowner_cache
    assert json.loads(journal.read_text()) == {'homeownerId':'someone-else'}
    assert not (tmp_path/'quote_requests/request.json').exists()
    assert not (tmp_path/'room_context/home').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200,503])
async def test_noop_or_failed_storage_deletion_never_removes_account(service, monkeypatch, status):
    async def rpc(name,user):
        assert name == 'account_deletion_manifest'
        return {**manifest(),'objects':[{'bucket':'home-assets','path':'owner/model.usdz'}]}
    def network(request):
        assert request.url.path == '/storage/v1/object/home-assets'
        return httpx.Response(status,json=[])
    monkeypatch.setattr(deletion,'_rpc',rpc)
    async with httpx.AsyncClient(base_url='https://supabase.invalid',transport=httpx.MockTransport(network)) as client:
        with pytest.raises((RuntimeError,httpx.HTTPStatusError)):
            await deletion.delete_account(service,'token',client)


@pytest.mark.asyncio
async def test_receipt_cleanup_outage_does_not_report_failed_account_deletion(service, monkeypatch):
    async def rpc(*args): return manifest()
    monkeypatch.setattr(deletion,'_rpc',rpc)
    def network(request):
        return httpx.Response(503 if request.url.path == '/account_deletions' else 200,json={})
    async with httpx.AsyncClient(base_url='https://supabase.invalid',transport=httpx.MockTransport(network)) as client:
        monkeypatch.setattr(supabase_store,'_rest',lambda:client)
        await deletion.delete_account(service,'token',client)


def test_local_cleanup_handles_partial_logs_and_preserves_other_accounts(service, tmp_path):
    directory = tmp_path / 'flow_journal'
    directory.mkdir()
    journal = directory / 'journal.jsonl'
    journal.write_text('{"homeownerId":"owner",\n' + '{"homeownerId":"foreign",\n' + 'null\n')
    database = tmp_path / 'quote_intake.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE intakes(owner TEXT, state TEXT)')
        connection.execute('CREATE TABLE active_conversations(owner TEXT)')
        connection.executemany('INSERT INTO intakes VALUES(?,?)', [('owner','private'),('foreign','keep')])
        connection.executemany('INSERT INTO active_conversations VALUES(?)', [('owner',),('foreign',)])
    deletion._remove_local_data(manifest())
    assert journal.read_text() == '{"homeownerId":"foreign",\nnull\n'
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT * FROM intakes').fetchall() == [('foreign','keep')]
        assert connection.execute('SELECT * FROM active_conversations').fetchall() == [('foreign',)]
