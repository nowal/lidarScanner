"""Lossless whole-home transport, with durable leases and atomic publication.

The phone sends one gzip. Downloadable USDZs are exact bytes from that archive;
we neither load a 3D scene nor re-encode geometry or textures.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import hashlib
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException

from ..config import settings
from . import home_registry, scan_uploads, supabase_store

CHUNK = 6 * 1024 * 1024
MAX_MODEL_BYTES = 1_048_576_000  # same limit as the existing model endpoint
BUCKET = 'metashape-exports'


async def transition(home: str, action: str, revision: str, owner: str, payload=None):
    return await scan_uploads.transition(home, action, revision=revision, owner=owner,
        payload=payload, rpc='scan_model_upload_transition')


def status(state: dict) -> dict:
    # Upload-session URLs stay private to the backend worker.
    progress = state.get('progress') or {'status': 'unknown'}
    result = {k: v for k, v in progress.items()
              if k in ('status', 'stage', 'error', 'retryable', 'completedModels', 'totalModels')}
    if result.get('status') in ('queued', 'running') and not state.get('leaseActive'):
        result = {'status': 'unknown'}
    return dict(result, revision=state['revision'])


async def begin(home: str, body, owner: str, background) -> dict:
    from ..flow_api import _check_scan_object
    package = body.model.model_dump()
    _check_scan_object(home, owner, package['bucket'], package['objectPath'], '.usdz.gz')
    if not re.fullmatch(r'home-[a-f0-9]{64}\.usdz\.gz', Path(package['objectPath']).name):
        raise HTTPException(422, detail='The home archive needs its immutable content hash')
    state = await transition(home, 'begin', body.revision, owner, {'package': package})
    token = str(uuid4())
    state = await transition(home, 'claim', body.revision, owner, {'token': token})
    if state['claimed']:
        background.add_task(prepare, home, body.revision, owner, token, state)
    return status(state)


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def unpack(archive: Path, directory: Path, package: dict, expected: set[str]) -> dict[str, Path]:
    """Bounded gzip decompression + fixed-name extraction; no extractall/paths from ZIP."""
    home = directory / 'home.usdz'
    total = 0
    digest = hashlib.sha256()
    try:
        with gzip.open(archive, 'rb') as source, home.open('wb') as target:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > min(package['uncompressedBytes'], MAX_MODEL_BYTES):
                    raise ValueError('The model archive exceeds its declared size')
                target.write(chunk)
                digest.update(chunk)
        if total != package['uncompressedBytes'] or digest.hexdigest() != package['sha256']:
            raise ValueError('The decompressed model does not match the saved USDZ')
        files = {'home': home}
        with zipfile.ZipFile(home) as bundle:
            names = bundle.namelist()
            if len(names) > 1000 or len(names) != len(set(names)) or names[0:1] != ['home.usda']:
                raise ValueError('The whole-home package layout is invalid')
            present = {m.group(1) for name in names
                       if (m := re.fullmatch(r'rooms/(room-[0-9]+)/model\.usdz', name))}
            if present != expected:
                raise ValueError('The whole-home model must contain every saved area')
            for key in sorted(expected):
                entry = bundle.getinfo(f'rooms/{key}/model.usdz')
                if entry.compress_type != zipfile.ZIP_STORED or not 0 < entry.file_size <= total:
                    raise ValueError('An embedded area model is invalid')
                destination = directory / f'{key}.usdz'
                with bundle.open(entry) as source, destination.open('wb') as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
                files[key] = destination
        return files
    except (gzip.BadGzipFile, EOFError, zipfile.BadZipFile) as exc:
        raise ValueError('The uploaded model archive is damaged') from exc


def tus_endpoint() -> str:
    parts = urlsplit(settings.supabase_url)
    host = parts.netloc
    if host.endswith('.supabase.co') and not host.endswith('.storage.supabase.co'):
        host = host.replace('.supabase.co', '.storage.supabase.co')
    return urlunsplit((parts.scheme, host, '/storage/v1/upload/resumable', '', ''))


async def upload_model(path: Path, object_path: str, session_url: str | None, save_session,
                       *, client: httpx.AsyncClient | None = None, content_type: str = 'model/vnd.usdz+zip') -> None:
    """Stream six-MB chunks; saved URLs survive backend restarts. Never overwrite."""
    size = path.stat().st_size
    try:
        if await supabase_store.stored_object_size(BUCKET, object_path) == size:
            return
        raise ValueError('An existing immutable model has a different size')
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (400, 404):
            raise
    endpoint = tus_endpoint()
    root = urlsplit(endpoint)

    def valid(url):
        parts = urlsplit(url)
        return (parts.scheme, parts.netloc) == (root.scheme, root.netloc) and parts.path.startswith(root.path + '/')

    headers = {'apikey': settings.supabase_service_role_key,
               'Authorization': f'Bearer {settings.supabase_service_role_key}', 'Tus-Resumable': '1.0.0'}
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=120.0, follow_redirects=False)
    try:
        offset = 0
        if session_url:
            if not valid(session_url): raise ValueError('Invalid saved upload session')
            head = await client.head(session_url, headers=headers)
            if head.status_code in (404, 410): session_url = None
            else:
                head.raise_for_status()
                offset = int(head.headers['upload-offset'])
        if not session_url:
            metadata = {'bucketName': BUCKET, 'objectName': object_path,
                        'contentType': content_type, 'cacheControl': '3600'}
            created = await client.post(endpoint, headers=dict(headers, **{
                'Upload-Length': str(size), 'Upload-Metadata': ','.join(
                    f'{k} {base64.b64encode(v.encode()).decode()}' for k, v in metadata.items())}))
            created.raise_for_status()
            session_url = urljoin(endpoint + '/', created.headers['location'])
            if not valid(session_url): raise ValueError('Invalid upload destination')
            await save_session(session_url)
        if not 0 <= offset <= size: raise ValueError('Invalid saved upload offset')
        failures = 0
        with path.open('rb') as source:
            while offset < size:
                source.seek(offset)
                chunk = source.read(CHUNK)
                try:
                    response = await client.patch(session_url, headers=dict(headers, **{
                        'Content-Type': 'application/offset+octet-stream', 'Upload-Offset': str(offset)}), content=chunk)
                    response.raise_for_status()
                    accepted = int(response.headers['upload-offset'])
                    if accepted != offset + len(chunk): raise ValueError('Invalid accepted upload offset')
                    offset = accepted
                    failures = 0
                except (httpx.HTTPError, ValueError):
                    if failures >= 3: raise
                    failures += 1
                    await asyncio.sleep(failures)
                    head = await client.head(session_url, headers=headers)
                    head.raise_for_status()
                    offset = int(head.headers['upload-offset'])
                    if not 0 <= offset <= size: raise ValueError('Invalid retry upload offset')
        if await supabase_store.stored_object_size(BUCKET, object_path) != size:
            raise RuntimeError('The prepared model upload is incomplete')
    finally:
        if own_client: await client.aclose()


def asset_row(home: str, key: str, path: str, size: int) -> dict:
    identifier = str(UUID(hex=hashlib.sha256(f'{BUCKET}/{path}'.encode()).hexdigest()[:32]))
    return {
            'id': identifier,
            'asset_type': 'lidar_model' if key == 'home' else 'scan_room_model',
            'storage_path': path, 'source': 'ios_automatic_scan_upload',
            'metadata_json': {'scan_id': home, 'file_name': Path(path).name, 'storage_bucket': BUCKET,
                'file_size_bytes': str(size), 'content_type': 'model/vnd.usdz+zip',
                'upload_stage': 'final_home_model' if key == 'home' else 'final_room_model'}}


async def prepare(home: str, revision: str, owner: str, token: str, state: dict) -> None:
    package = state['package']
    progress = dict(state['progress'], status='running')
    sessions = progress.setdefault('uploads', {})
    lost = asyncio.Event()

    async def checkpoint():
        if lost.is_set(): raise RuntimeError('The model preparation lease was lost')
        await transition(home, 'heartbeat', revision, owner, {'token': token, 'progress': progress})

    async def heartbeat():
        while True:
            await asyncio.sleep(25)
            try: await checkpoint()
            except Exception:
                lost.set()
                return

    pulse = asyncio.create_task(heartbeat())
    work = Path(tempfile.mkdtemp(prefix='home-model-'))
    try:
        current = await scan_uploads.transition(home, 'read', owner=owner)
        if current['revision'] != revision: return
        expected = {room['key'] for room in current['contextIndex']['rooms']}
        if not expected or any(not re.fullmatch(r'room-[0-9]+', key) for key in expected):
            raise ValueError('The saved scan has invalid area identifiers')
        archive = work / 'home.usdz.gz'
        progress.update(stage='downloading_model')
        await checkpoint()
        if not await supabase_store.download_object(package['bucket'], package['objectPath'], archive, max_bytes=package['bytes']):
            raise RuntimeError('The uploaded home archive could not be downloaded')
        if archive.stat().st_size != package['bytes']:
            raise ValueError('The uploaded home archive is incomplete')
        compressed_hash = await asyncio.to_thread(fingerprint, archive)
        if Path(package['objectPath']).name != f'home-{compressed_hash}.usdz.gz':
            raise ValueError('The uploaded home archive does not match its content hash')
        progress.update(stage='unpacking_model')
        await checkpoint()
        files = await asyncio.to_thread(unpack, archive, work, package, expected)
        models = {}
        assets = []
        progress.update(stage='saving_models', completedModels=0, totalModels=len(files))
        for key, file in files.items():
            await checkpoint()
            digest = await asyncio.to_thread(fingerprint, file)
            object_path = f'{owner}/{home}/{key}-{digest}.usdz'

            async def save_session(url, key=key):
                sessions[key] = url
                await checkpoint()

            await upload_model(file, object_path, sessions.get(key), save_session)
            await checkpoint()
            size = file.stat().st_size
            assets.append(asset_row(home, key, object_path, size))
            models[key] = {'bucket': BUCKET, 'object': object_path, 'bytes': size,
                          'uploadedAt': home_registry.now_iso(), 'file': f'{key}.usdz'}
            progress['completedModels'] += 1
        await transition(home, 'complete', revision, owner, {'token': token, 'models': models, 'assets': assets})
    except Exception as exc:
        scan_uploads.logger.warning('Home model preparation failed %s revision=%s (%s)', home, revision, type(exc).__name__)
        progress.update(status='failed', error=(str(exc)[:200] if isinstance(exc, ValueError)
                        else 'The uploaded model is saved. Retry preparing its downloads.'),
                        retryable=not isinstance(exc, ValueError))
        with contextlib.suppress(Exception):
            await transition(home, 'fail', revision, owner, {'token': token, 'progress': progress})
    finally:
        pulse.cancel()
        with contextlib.suppress(asyncio.CancelledError): await pulse
        shutil.rmtree(work, ignore_errors=True)
