"""Durable compare-and-swap storage. Supabase failures never fall back locally.

Each claim increments the storage version before doing external work. A lease
limits single flight across workers; CAS fences any late response. Conversation
revision is separate, advancing only for completed homeowner turns.
"""
import asyncio
import json
import sqlite3
from pathlib import Path
from fastapi import HTTPException
from ..config import settings
from ..flow import supabase_store


def _db():
    path = Path(settings.storage_dir) / 'quote_intake.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.execute('CREATE TABLE IF NOT EXISTS intakes (id TEXT PRIMARY KEY, owner TEXT NOT NULL, home TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL)')
    return connection


def _local_read(identifier):
    with _db() as connection:
        row = connection.execute('SELECT owner, home, version, state FROM intakes WHERE id=?', (identifier,)).fetchone()
    return None if row is None else dict(id=identifier, owner_id=row[0], home_id=row[1], version=row[2], state=json.loads(row[3]))


def _local_cas(row, previous):
    with _db() as connection:
        if previous is None:
            cursor = connection.execute('INSERT OR IGNORE INTO intakes VALUES(?,?,?,?,?)',
                (row['id'], row['owner_id'], row['home_id'], row['version'], json.dumps(row['state'])))
        else:
            cursor = connection.execute('UPDATE intakes SET state=?, version=? WHERE id=? AND owner=? AND home=? AND version=?',
                (json.dumps(row['state']), row['version'], row['id'], row['owner_id'], row['home_id'], previous))
        return cursor.rowcount == 1


async def read(identifier: str, owner: str, home: str):
    try:
        if supabase_store.enabled():
            response = await supabase_store._rest().get('/quote_intakes', params={'id': 'eq.' + identifier, 'select': '*'})
            response.raise_for_status()
            rows = response.json()
            row = rows[0] if rows else None
        else:
            row = await asyncio.to_thread(_local_read, identifier)
    except Exception as exc:
        raise HTTPException(503, 'Intake storage is unavailable. Your saved answers have not changed; retry.') from exc
    if row and (row['owner_id'] != owner or row['home_id'] != home):
        raise HTTPException(404, 'Intake not found for this homeowner and scan')
    return row


async def save(identifier: str, owner: str, home: str, state: dict, previous: int | None) -> int:
    version = 0 if previous is None else previous + 1
    row = dict(id=identifier, owner_id=owner, home_id=home, version=version, state=state)
    try:
        if supabase_store.enabled():
            if previous is None:
                response = await supabase_store._rest().post('/quote_intakes', json=row,
                    headers={'Prefer': 'resolution=ignore-duplicates,return=representation'})
            else:
                response = await supabase_store._rest().patch('/quote_intakes',
                    params={'id': 'eq.' + identifier, 'owner_id': 'eq.' + owner, 'home_id': 'eq.' + home,
                            'version': f'eq.{previous}'},
                    json={'version': version, 'state': state}, headers={'Prefer': 'return=representation'})
            response.raise_for_status()
            changed = bool(response.json())
        else:
            changed = await asyncio.to_thread(_local_cas, row, previous)
    except Exception as exc:
        raise HTTPException(503, 'Could not save this intake. Retry the same turn.') from exc
    if not changed:
        raise HTTPException(409, 'This intake changed in another request. Reload and retry.')
    return version
