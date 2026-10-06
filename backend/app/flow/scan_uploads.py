"""Durable staged uploads: immutable artifacts, shared leases, fenced publication.

The database is authoritative across processes. No pending build writes the live
Storage index; the previous published model set remains referenced until commit.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile

import httpx
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException

from ..config import settings
from ..home_index import HomeIndex, load_bundle
from .. import room_context
from . import supabase_store

logger = logging.getLogger(__name__)


async def transition(home_id: str, action: str, *, revision: str | None = None,
                     owner: str | None = None, payload: dict | None = None,
                     rpc: str = 'scan_upload_transition') -> dict | None:
    if not supabase_store.enabled():
        return None
    try:
        UUID(home_id)
    except ValueError:
        if action == 'read': return None  # pre-library/CLI home identifiers
        raise HTTPException(422, detail='Invalid scan identifier')
    try:
        response = await supabase_store._rest().post('/rpc/' + rpc, json={
            'p_home_id': home_id, 'p_action': action, 'p_revision': revision,
            'p_owner_id': owner, 'p_payload': payload or {}})
        response.raise_for_status()
        result = response.json()
    except Exception as exc:
        # Never fall back to a process-local answer after a DB failure.
        logger.warning('Scan upload state unavailable for %s (%s)', home_id, type(exc).__name__)
        raise HTTPException(503, detail={'code': 'upload_state_unavailable', 'retryable': True,
                                        'message': 'Upload progress is saved. Retrying the connection.'}) from exc
    if result is None and action not in ('read', 'delete'):
        raise HTTPException(409, detail={'code': 'revision_mismatch', 'retryable': False,
                                        'message': 'This scan upload no longer exists.'})
    if result and result.get('error'):
        code = result['error']
        raise HTTPException(403 if code == 'owner_mismatch' else 409, detail={
            'code': code, 'retryable': code in ('context_not_ready', 'lease_lost'),
            'message': ('A newer build has replaced this upload.' if code == 'revision_mismatch'
                        else 'This upload is not ready to publish. Retry shortly.')})
    return result


def status(state: dict, object_path: str) -> dict:
    if state.get('objectPath') != object_path:
        return {'status': 'superseded', 'error': 'A newer build has replaced this upload.'}
    result = dict(state['progress'])
    if result.get('status') in ('queued', 'running') and not state.get('leaseActive'):
        result = {'status': 'unknown'}  # phone resubmits; one worker reclaims lease
    result['revision'] = state['revision']
    return result


def visible_index(state: dict) -> dict | None:
    # New context may be used by chat before models finish. Its readiness still
    # blocks NEW quote submissions. Old published links remain intact separately.
    return state.get('contextIndex') or state.get('publishedIndex')


async def begin(home_id: str, body, owner: str, background) -> dict:
    from . import home_registry
    previous = await home_registry.load_index_async(home_id)
    state = await transition(home_id, 'begin', revision=body.revision, owner=owner, payload={
        'objectPath': body.objectPath, 'generation': body.generation,
        'previous_index': previous.to_json() if previous else None})
    token = str(uuid4())
    claimed = await transition(home_id, 'claim', revision=body.revision, owner=owner, payload={'token': token})
    if claimed['claimed']:
        background.add_task(ingest, home_id, body.bucket, body.objectPath, body.revision, owner, token, body.enrich)
    return status(claimed, body.objectPath)


async def ingest(home_id: str, bucket: str, object_path: str, revision: str,
                 owner: str, token: str, enrich: bool) -> None:
    from . import home_registry
    progress = {'status': 'running', 'stage': 'reading_scan'}
    lost = asyncio.Event()

    async def heartbeat():
        while True:
            await asyncio.sleep(25)
            try:
                await transition(home_id, 'heartbeat', revision=revision, owner=owner,
                                 payload={'token': token, 'progress': dict(progress)})
            except Exception:
                lost.set()
                return

    pulse = asyncio.create_task(heartbeat())
    work = Path(tempfile.mkdtemp(prefix='scan-context-'))
    try:
        archive = work / 'context.zip'
        if not await supabase_store.download_object(bucket, object_path, archive):
            raise RuntimeError('The saved context package could not be downloaded.')
        bundle = await asyncio.to_thread(home_registry.unpack_export, archive, work)
        index = await asyncio.to_thread(load_bundle, bundle)
        if not index.rooms:
            raise ValueError('The scan export has no saved rooms')
        state = await transition(home_id, 'read', owner=owner)
        if state['revision'] != revision:
            return
        old = state.get('previousIndex') or state.get('publishedIndex')
        previous = HomeIndex.from_json(old) if old else None
        progress.update(stage='analyzing_photos', totalRooms=len(index.rooms), completedRooms=0)
        for n, room in enumerate(index.rooms):
            if lost.is_set():
                return
            document = await asyncio.to_thread(room_context.geometry_context, bundle, room.key)
            room.measurements = dict(document.get('measurements') or {})
            if enrich and settings.anthropic_api_key:
                try:
                    # Build into this revision's private object, never a shared
                    # room document or the current home index.
                    document = await room_context.build(bundle, room.key)
                except Exception as exc:
                    logger.warning('Photo analysis unavailable for %s/%s (%s)', home_id, room.key, type(exc).__name__)
            room.appearance = dict(document)
            if document.get('setting') == 'exterior':
                room.role, room.display_name = 'exterior', 'exterior'
                room.name_basis, room.confident = 'the photos show the outside of the house', True
            prior = previous.by_key(room.key) if previous else None
            if prior:
                room.model = dict(prior.model)
                if prior.named_by_homeowner:
                    index.rename_room(room.key, prior.display_name)
            progress['completedRooms'] = n + 1
            await transition(home_id, 'heartbeat', revision=revision, owner=owner,
                             payload={'token': token, 'progress': dict(progress)})
        if previous:
            index.home_model = dict(previous.home_model)
        index.upload = {'revision': revision, 'ownerId': owner, 'contextObject': object_path,
                        'contextBucket': bucket, 'contextReady': True, 'modelsReady': False, 'durableV2': True}
        await transition(home_id, 'context_complete', revision=revision, owner=owner,
                         payload={'token': token, 'index': index.to_json()})
        logger.info('Completed durable context %s revision=%s rooms=%s', home_id, revision, len(index.rooms))
    except Exception as exc:
        logger.warning('Durable context failed %s revision=%s (%s)', home_id, revision, type(exc).__name__)
        with contextlib.suppress(Exception):
            await transition(home_id, 'fail', revision=revision, owner=owner, payload={
                'token': token, 'progress': {'status': 'failed', 'error': str(exc)[:250],
                    'retryable': isinstance(exc, (httpx.HTTPError, RuntimeError))}})
    finally:
        pulse.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pulse
        shutil.rmtree(work, ignore_errors=True)


async def publish(home_id: str, body, owner: str) -> dict:
    from ..flow_api import _check_scan_object
    from . import home_registry
    state = await transition(home_id, 'read', owner=owner)
    if not state or state['revision'] != body.revision:
        raise HTTPException(409, detail={'code': 'revision_mismatch', 'retryable': False,
                                        'message': 'A newer build has replaced this upload.'})
    raw = state.get('contextIndex')
    if not raw:
        raise HTTPException(409, detail={'code': 'context_not_ready', 'retryable': True,
                                        'message': 'Files uploaded. Waiting for scan context.'})
    expected = {r['key'] for r in raw['rooms']} | {'home'}
    if len(body.models) != len(expected) or {m.key for m in body.models} != expected:
        raise HTTPException(422, detail='Upload the final home model and every area’s model')
    models = {}
    for model in body.models:
        _check_scan_object(home_id, owner, model.bucket, model.objectPath, '.usdz')
        try:
            size = await supabase_store.stored_object_size(model.bucket, model.objectPath)
        except Exception as exc:
            raise HTTPException(503, detail='Could not verify the uploaded model. Retry shortly.') from exc
        if size != model.bytes:
            raise HTTPException(422, detail=f'Model {model.key} upload is incomplete')
        models[model.key] = {'bucket': model.bucket, 'object': model.objectPath, 'bytes': size,
                             'uploadedAt': home_registry.now_iso(), 'file': f'{model.key}.usdz'}
    # DB checks the revision AGAIN after the network awaits, and atomically
    # publishes all links from the latest context. The former published set is
    # never overwritten file-by-file.
    await transition(home_id, 'publish', revision=body.revision, owner=owner, payload={'models': models})
    return {'status': 'done', 'modelCount': len(models)}


async def save_names(home_id: str, payload: dict) -> None:
    names = {r['key']: {k: r[k] for k in ('name', 'nameBasis', 'confident', 'namedByHomeowner') if k in r}
             for r in payload['rooms'] if r.get('namedByHomeowner')}
    await transition(home_id, 'rename', revision=payload['upload']['revision'], payload={'names': names})
