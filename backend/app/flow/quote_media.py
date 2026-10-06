"""Revision-pinned quote media and private, restart-safe derivative storage.

The quote JSON is the durable snapshot; all paths come from the published index,
never from URL parameters. Viewer capabilities cannot enter quotes or issue ops
commands. The existing private scan folder/deletion lifecycle owns derivatives.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import re
import tempfile
import time
import zipfile
from pathlib import Path

import httpx
from fastapi import HTTPException

from ..config import settings
from ..home_index import HomeIndex
from . import supabase_store, scan_uploads

logger = logging.getLogger(__name__)
TTL = 30 * 86400
BUCKET = 'metashape-exports'
CONVERTER_VERSION = 'usd25.11-glb-v2'
_tasks: dict[str, asyncio.Task] = {}
_slots = asyncio.Semaphore(1)


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


async def capture(record) -> dict:
    """One atomic published index read; never combine pending context with models."""
    if not record.homeId:
        return {}
    state = await scan_uploads.transition(record.homeId, 'read')
    if not state or not state.get('publishedIndex'):
        return {'version': 1, 'reason': 'No published scan media is available.'}
    if record.homeownerId and state.get('ownerId') != record.homeownerId:
        raise HTTPException(403, 'This scan belongs to another homeowner')
    raw = state['publishedIndex']
    upload = raw.get('upload') or {}
    if not upload.get('modelsReady') or not upload.get('revision'):
        return {'version': 1, 'reason': 'No complete scan revision is available.'}
    if state.get('revision') != upload['revision']:
        # The submit gate raced a new build. Do not silently quote the old scan.
        raise HTTPException(409, detail='A scan update is still uploading. Submit after it finishes.')
    index = HomeIndex.from_json(raw)
    if record.scopeIntent == 'whole_home':
        rooms = index.rooms
    elif record.scopeIntent == 'selected_rooms':
        rooms = []
        for name in record.scopeRooms:
            matches = [r for r in index.rooms if r.key == name or r.display_name.casefold() == name.casefold()]
            if len(matches) != 1:
                return {'version': 1, 'reason': 'The requested rooms could not be matched unambiguously to the scan.'}
            if matches[0] not in rooms: rooms.append(matches[0])
    elif record.roomKey:
        rooms = [r for r in index.rooms if r.key == record.roomKey]
    else:
        rooms = index.rooms
    models = []
    if record.scopeIntent == 'whole_home' or (not record.roomKey and record.scopeIntent != 'selected_rooms'):
        candidates = [('home', 'Whole home', index.home_model)]
    else:
        candidates = [(r.key, r.display_name, r.model) for r in rooms]
    for key, label, m in candidates:
        if m.get('bucket') == BUCKET and re.search(r'-[a-f0-9]{64}\.usdz$', m.get('object', '')):
            models.append(dict(key=key, label=label, bucket=BUCKET, object=m['object'], bytes=m.get('bytes', 0)))
    media = {'version': 1, 'homeId': record.homeId, 'ownerId': upload.get('ownerId') or state.get('ownerId'),
             'revision': upload['revision'], 'context': {'bucket': upload.get('contextBucket', BUCKET), 'object': upload.get('contextObject')},
             'rooms': [{'key': r.key, 'label': r.display_name, 'frameIds': list(r.frame_ids)} for r in rooms],
             'models': models, 'photos': []}
    media['snapshotId'] = _hash(media)
    return media


def signature(record, exp: int) -> str:
    secret = settings.flow_token_secret or settings.auth_token
    if not secret: raise ValueError('Private media signing is not configured')
    payload = f"quote-media-v1:{record.id}:{record.scanMedia.get('snapshotId')}:{exp}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def link(record) -> str | None:
    if not settings.public_base_url or not record.scanMedia.get('snapshotId'):
        return None
    # Stable expiry/payload across retries also preserves Resend idempotency.
    exp = record.scanMedia.get('expiresAt')
    if not exp: return None
    return f'{settings.public_base_url.rstrip("/")}{settings.api_prefix}/quote-media/{record.id}/{exp}/{signature(record, exp)}'


async def authorized(request_id: str, exp: int, sig: str):
    from ..flow_quotes import quote_store
    if exp <= int(time.time()): raise HTTPException(410, 'This scan link has expired. Request a new email from TakeShape.')
    record = await quote_store.get(request_id)
    if not record or not record.scanMedia.get('snapshotId'): raise HTTPException(404, 'Scan media not found')
    if exp != record.scanMedia.get('expiresAt') or not hmac.compare_digest(signature(record, exp), sig):
        raise HTTPException(403, 'Invalid scan link')
    if supabase_store.enabled():
        state = await scan_uploads.transition(record.homeId, 'read')
        if not state or state.get('ownerId') != record.scanMedia.get('ownerId'):
            raise HTTPException(404, 'This scan is no longer available')
    return record


def prefix(media) -> str:
    # The snapshot only originates from the trusted published scan record.
    from uuid import UUID
    return f"{UUID(media['ownerId'])}/{UUID(media['homeId'])}/"


async def put_bytes(path: str, data: bytes, content_type: str, *, replace=False):
    if not supabase_store.enabled(): raise RuntimeError('Private storage is not configured')
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(f'{settings.supabase_url.rstrip("/")}/storage/v1/object/{BUCKET}/{path}',
            headers={'Authorization': f'Bearer {settings.supabase_service_role_key}',
                     'apikey': settings.supabase_service_role_key, 'Content-Type': content_type,
                     'x-upsert': 'true' if replace else 'false'}, content=data)
        if response.status_code == 409 and not replace: return
        response.raise_for_status()


async def get_json(path: str) -> dict:
    if not supabase_store.enabled(): return {}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f'{settings.supabase_url.rstrip("/")}/storage/v1/object/{BUCKET}/{path}',
            headers={'Authorization': f'Bearer {settings.supabase_service_role_key}'})
        if response.status_code in (400, 404): return {}
        response.raise_for_status()
        return response.json()


def photo_bytes(archive: Path, media: dict) -> list[tuple[dict, bytes, bytes]]:
    """Read selected JPEGs from durable context ZIP, rotate using camera gravity."""
    from PIL import Image
    from ..frame_select import upright_rotation
    result = []
    with zipfile.ZipFile(archive) as z:
        members = z.infolist()
        if len(members) > 5000 or sum(i.file_size for i in members) > 128 * 1024 * 1024:
            raise ValueError('Context package exceeds image limits')
        frames = {}
        for item in members:
            if re.fullmatch(r'(?:[^/]+/)?rooms/room-\d+/rebuild/manifest\.json', item.filename):
                manifest = json.loads(z.read(item))
                base = item.filename.rsplit('/', 1)[0]
                for frame in manifest.get('frames', []):
                    fid = str(frame.get('id', ''))
                    if re.fullmatch(r'[A-Za-z0-9_-]{1,80}', fid):
                        frames[fid] = (f'{base}/images/{fid}.jpg', frame)
        # Round robin: representative first view from every in-scope area.
        for n in range(4):
            for room in media['rooms']:
                ids = room['frameIds']
                if n >= len(ids) or ids[n] not in frames: continue
                name, frame = frames[ids[n]]
                if name not in z.namelist(): continue
                try:
                    with Image.open(io.BytesIO(z.read(name))) as image:
                        if image.width * image.height > 16_000_000: continue
                        image = image.convert('RGB')
                        pose = frame.get('cameraTransform') or []
                        turns = upright_rotation(pose) if len(pose) == 16 else 0
                        if turns: image = image.rotate(90 * turns, expand=True)
                        image.thumbnail((768, 768))
                        full = io.BytesIO(); image.save(full, format='JPEG', quality=88)
                        image.thumbnail((360, 270))
                        thumb = io.BytesIO(); image.save(thumb, format='JPEG', quality=82)
                    result.append(({'roomKey': room['key'], 'label': f"{room['label']} · view {n + 1}", 'frameId': ids[n]}, full.getvalue(), thumb.getvalue()))
                except (OSError, ValueError):
                    continue
    return result


async def prepare_email(record):
    """Cheap signing + bounded photo preparation; model conversion is queued separately."""
    media = record.scanMedia
    if not media.get('snapshotId'): return
    from ..flow_quotes import quote_store
    media.setdefault('expiresAt', int(time.time()) + TTL)
    context = media['context']
    if not media.get('photosPrepared') and context.get('object'):
        try:
            with tempfile.TemporaryDirectory(prefix='quote-photos-') as tmp:
                archive = Path(tmp) / 'context.zip'
                if not await supabase_store.download_object(context['bucket'], context['object'], archive, max_bytes=32 * 1024 * 1024):
                    raise RuntimeError('Context photos could not be retrieved')
                photos = await asyncio.to_thread(photo_bytes, archive, media)
                entries = []
                for info, full, thumb in photos:
                    key = _hash([media['snapshotId'], info['frameId'], 'upright-v1'])
                    obj = prefix(media) + f'quote-photo-{key}.jpg'
                    small = prefix(media) + f'quote-thumb-{key}.jpg'
                    await put_bytes(obj, full, 'image/jpeg'); await put_bytes(small, thumb, 'image/jpeg')
                    entries.append(dict(info, object=obj, thumbnail=small))
                media['photos'] = entries
                media['photosPrepared'] = True
        except Exception as exc:
            logger.warning('Quote photos unavailable for %s (%s)', record.id, type(exc).__name__)
    await quote_store.save(record)
    viewer = link(record)
    if viewer:
        record.modelLink = {'kind': 'quote_media', 'viewerUrl': viewer,
                            'url': viewer + '/models/0/usdz' if media['models'] else None,
                            'note': 'Private scan gallery and desktop viewer; link expires in 30 days.'}
        await quote_store.save(record)
    if media['models']: queue_conversion(media, media['models'][0])


def derivative_path(media, model) -> str:
    return prefix(media) + 'quote-model-' + _hash([CONVERTER_VERSION, model['bucket'], model['object']]) + '.glb'


def queue_conversion(media, model):
    key = derivative_path(media, model)
    if key not in _tasks:
        task = asyncio.create_task(_convert(media, model))
        _tasks[key] = task
        task.add_done_callback(lambda t: _tasks.pop(key, None))


async def conversion_status(media, model) -> dict:
    path = derivative_path(media, model)
    state = await get_json(path + '.json')
    if state.get('status') == 'ready': return state
    if state.get('status') == 'failed' and time.time() - state.get('updatedAt', 0) < 3600:
        return {'status': 'failed'}
    queue_conversion(media, model)
    return {'status': 'preparing'}


async def _convert(media, model):
    """One bounded subprocess at a time. Durable ready receipt only follows upload.

    A process killed by deploy leaves a preparing receipt. The next viewer visit
    requeues it; completed derivatives are reused across requests/restarts.
    """
    import sys
    from .model_archive import upload_model
    path = derivative_path(media, model)
    process = None
    async with _slots:
        try:
            status = await get_json(path + '.json')
            if status.get('status') == 'ready': return
            if status.get('status') == 'failed' and time.time() - status.get('updatedAt', 0) < 3600: return
            await put_bytes(path + '.json', json.dumps({'status': 'preparing', 'updatedAt': time.time()}).encode(), 'application/json', replace=True)
            with tempfile.TemporaryDirectory(prefix='quote-model-') as tmp:
                source, target = Path(tmp) / 'source.usdz', Path(tmp) / 'model.glb'
                if not await supabase_store.download_object(model['bucket'], model['object'], source, max_bytes=min(model['bytes'] or 1_048_576_000, 1_048_576_000)):
                    raise RuntimeError('Model download unavailable')
                digest = await asyncio.to_thread(_file_hash, source)
                if not model['object'].endswith('-' + digest + '.usdz'):
                    raise ValueError('Model content does not match its immutable name')
                process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'app.flow.usdz_to_glb', str(source), str(target),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=1200)
                if process.returncode != 0: raise ValueError('Model conversion failed')
                stats = json.loads(stdout)
                # Do not resurrect derivatives after homeowner deletion.
                state = await scan_uploads.transition(media['homeId'], 'read')
                if not state or state.get('ownerId') != media['ownerId']: return
                async def session(_): pass
                await upload_model(target, path, None, session, content_type='model/gltf-binary')
                await put_bytes(path + '.json', json.dumps({'status': 'ready', 'updatedAt': time.time(), 'stats': stats}).encode(), 'application/json', replace=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('Desktop conversion unavailable (%s)', type(exc).__name__)
            try:
                await put_bytes(path + '.json', json.dumps({'status': 'failed', 'updatedAt': time.time()}).encode(), 'application/json', replace=True)
            except Exception: pass
        finally:
            if process and process.returncode is None:
                process.kill(); await process.wait()


def _file_hash(path):
    with path.open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()
