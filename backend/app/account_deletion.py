"""Authenticated self-service hard deletion; credentials stay on the server."""
import asyncio
import json
import logging
import re
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path
from uuid import UUID
from weakref import WeakValueDictionary

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Response

from .config import settings
from .flow import home_registry, supabase_store
from .flow_api import require_token

router = APIRouter(prefix=settings.api_prefix)
logger = logging.getLogger(__name__)
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _headers():
    return {'apikey': settings.supabase_service_role_key,
            'Authorization': 'Bearer ' + settings.supabase_service_role_key}


async def _rpc(name, user):
    response = await supabase_store._rest().post('/rpc/' + name, json={'p_user': user})
    response.raise_for_status()
    return response.json() if response.content else None


async def _verified_user(token: str | None, client: httpx.AsyncClient) -> str:
    if not token:
        raise HTTPException(401, 'Sign in to delete your account.')
    response = await client.get('/auth/v1/user', headers={'Authorization': 'Bearer ' + token})
    if response.status_code in (401, 403, 404):
        raise HTTPException(401, 'Please sign in again before deleting your account.')
    response.raise_for_status()
    # No caller-supplied user ID, email, metadata, or unverified JWT claims.
    return str(UUID(response.json()['id']))


def _safe(value):
    return re.sub(r'[^A-Za-z0-9_-]', '_', value)


def _remove_local_data(manifest):
    """Remove local fallback copies too, so an outage cannot restore them."""
    root = Path(settings.storage_dir)
    owners, threads = set(manifest['owners']), set(manifest['threads'])
    database = root / 'quote_intake.sqlite3'
    if database.exists():
        with sqlite3.connect(database) as connection:
            connection.execute('PRAGMA secure_delete=ON')
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('intakes', 'active_conversations'):
                if table in tables:
                    connection.executemany(f'DELETE FROM {table} WHERE owner=?', [(owner,) for owner in owners])
    for home in manifest['homes']:
        home_registry._cache.pop(home, None)
        (root / 'homes' / (_safe(home) + '.json')).unlink(missing_ok=True)
        path = root / 'room_context' / _safe(home)
        if path.exists():
            shutil.rmtree(path)
    for request in manifest['requests']:
        (root / 'quote_requests' / (_safe(request) + '.json')).unlink(missing_ok=True)
        path = root / 'provider_responses' / _safe(request)
        if path.exists():
            shutil.rmtree(path)
    for thread in threads:
        for folder, suffix in [('flow_state', '.json'), ('flow_state', '.opening.json'), ('flow_journal', '.jsonl')]:
            (root / folder / (_safe(thread) + suffix)).unlink(missing_ok=True)
    journal = root / 'flow_journal' / 'journal.jsonl'
    if journal.exists():
        # Synchronous, atomic replacement on this service's event loop.
        temporary = journal.with_suffix('.deleting')
        try:
            with journal.open() as source, temporary.open('w') as target:
                for line in source:
                    try:
                        record = json.loads(line)
                        belongs = isinstance(record, dict) and (
                            record.get('homeownerId') in owners or record.get('threadId') in threads)
                    except json.JSONDecodeError:
                        # Interrupted appends must not block deletion. Remove
                        # a partial record when its ownership ID is present.
                        belongs = any(json.dumps(identifier) in line for identifier in owners | threads)
                    if not belongs:
                        target.write(line)
            temporary.replace(journal)
        finally:
            temporary.unlink(missing_ok=True)


async def delete_account(user: str, token: str, client: httpx.AsyncClient):
    lock = _locks.setdefault(user, asyncio.Lock())
    async with lock:
        previous = None
        # Re-query the first batch after each removal: an offset would skip
        # files as the result set shrinks. Fail closed on a no-op deletion.
        while True:
            manifest = await _rpc('account_deletion_manifest', user)
            objects = manifest['objects']
            if not objects:
                break
            if objects == previous:
                raise RuntimeError('Storage deletion made no progress')
            previous = objects
            buckets = defaultdict(list)
            for obj in objects:
                buckets[obj['bucket']].append(obj['path'])
            for bucket, paths in buckets.items():
                response = await client.request('DELETE', '/storage/v1/object/' + bucket,
                                                json={'prefixes': paths})
                response.raise_for_status()

        _remove_local_data(manifest)
        await _rpc('account_deletion_remove_profiles', user)
        supabase_store.invalidate_homeowner(user)
        # Global logout revokes refresh sessions. Hard deletion removes Auth
        # identities; API requests also validate the user with Auth each time.
        response = await client.post('/auth/v1/logout?scope=global',
                                     headers={'Authorization': 'Bearer ' + token})
        if response.status_code not in (200, 204, 401, 403, 404):
            response.raise_for_status()
        response = await client.request('DELETE', '/auth/v1/admin/users/' + user,
                                        json={'should_soft_delete': False})
        response.raise_for_status()
        # The account is already gone. A receipt cleanup outage must not tell
        # the person that their deletion failed; no content is in this receipt.
        try:
            receipt = await supabase_store._rest().delete('/account_deletions', params={'auth_user_id': 'eq.' + user})
            receipt.raise_for_status()
        except httpx.HTTPError:
            logger.warning('Account deletion receipt cleanup needs retry')


@router.delete('/account', dependencies=[Depends(require_token)], status_code=204)
async def remove_account(x_homeowner_token: str | None = Header(default=None)):
    if not supabase_store.enabled():
        raise HTTPException(503, 'Account deletion is temporarily unavailable. Please try again.')
    try:
        async with httpx.AsyncClient(base_url=settings.supabase_url.rstrip('/'),
                                     headers=_headers(), timeout=60) as client:
            user = await _verified_user(x_homeowner_token, client)
            await delete_account(user, x_homeowner_token, client)
    except HTTPException:
        raise
    except Exception as error:
        logger.warning('Account deletion incomplete (%s)', type(error).__name__)
        raise HTTPException(503, 'Account deletion could not finish. Please try again to complete it.') from error
    return Response(status_code=204)
