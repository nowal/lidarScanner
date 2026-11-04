"""Immutable acceptance + independent delivery receipts, shared by all workers.

The parent request remains a historical record. This receipt is authoritative
when stale parent writes or late immutable provider responses arrive.
"""
import asyncio
import json
import sqlite3
import time
from pathlib import Path

from fastapi import HTTPException
from ..config import settings
from . import supabase_store


def db():
    path = Path(settings.storage_dir) / 'quote_lifecycle.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS acceptances (request_id TEXT PRIMARY KEY, quote TEXT NOT NULL, provider_delivered TEXT, provider_captured TEXT, homeowner_delivered TEXT, homeowner_captured TEXT, provider_claim TEXT, homeowner_claim TEXT, provider_until REAL, homeowner_until REAL)')
    return conn


async def rpc(name, args):
    try:
        response = await supabase_store._rest().post('/rpc/' + name, json=args)
        if response.status_code == 409:
            raise HTTPException(409, 'This request changed. Reload the current conversation.')
        if response.status_code == 404:
            raise HTTPException(404, 'Request not found for this homeowner and home')
        response.raise_for_status()
        return response.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, 'Could not save or restore this request. Please retry.') from exc


def _read(request_id):
    with db() as conn:
        row = conn.execute('SELECT * FROM acceptances WHERE request_id=?', (request_id,)).fetchone()
        return dict(row) if row else None


async def read(request_id):
    if not supabase_store.enabled():
        row = await asyncio.to_thread(_read, request_id)
        if row:
            row['quote'] = json.loads(row['quote'])
        return row
    try:
        response = await supabase_store._rest().get('/flow_quote_acceptances', params={'request_id': 'eq.' + request_id, 'select': '*'})
        response.raise_for_status()
        rows = response.json()
        return rows[0] if rows else None
    except Exception as exc:
        raise HTTPException(503, 'Could not restore the saved acceptance. Please retry.') from exc


def apply(record, row):
    if not row:
        return record
    from ..flow_quotes import ReturnedQuote
    quote = ReturnedQuote.model_validate(row['quote'])
    quote.acceptanceNotificationDeliveredAt = row.get('provider_delivered')
    quote.acceptanceNotificationCapturedAt = row.get('provider_captured')
    quote.homeownerConfirmationDeliveredAt = row.get('homeowner_delivered')
    quote.homeownerConfirmationCapturedAt = row.get('homeowner_captured')
    record.selectedQuoteId = quote.id
    record.quotes = [quote if q.id == quote.id else q for q in record.quotes]
    if not record.quote_by_id(quote.id):
        record.quotes.append(quote)
    return record


def _commit(record, quote):
    with db() as conn:
        conn.execute('INSERT OR IGNORE INTO acceptances(request_id,quote,provider_delivered,provider_captured,homeowner_delivered,homeowner_captured) VALUES(?,?,?,?,?,?)',
            (record.id, quote.model_dump_json(), quote.acceptanceNotificationDeliveredAt, quote.acceptanceNotificationCapturedAt,
             quote.homeownerConfirmationDeliveredAt, quote.homeownerConfirmationCapturedAt))
        row = dict(conn.execute('SELECT * FROM acceptances WHERE request_id=?', (record.id,)).fetchone())
        row['quote'] = json.loads(row['quote'])
        if row['quote']['id'] != quote.id:
            raise HTTPException(409, 'Another quote has already been accepted.')
        return row


async def commit(record, quote):
    if supabase_store.enabled():
        row = await rpc('accept_quote_once', dict(p_request=record.id, p_owner=record.homeownerId,
            p_home=record.homeId, p_quote=quote.model_dump(mode='json')))
    else:
        row = await asyncio.to_thread(_commit, record, quote)
    return apply(record, row)


def _delivery(request_id, channel, claim, result):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM acceptances WHERE request_id=?', (request_id,)).fetchone()
        if not row:
            return False
        if result is None:
            if row[channel + '_delivered'] or (row[channel + '_until'] or 0) > time.time():
                return False
            conn.execute(f'UPDATE acceptances SET {channel}_claim=?, {channel}_until=? WHERE request_id=?', (claim, time.time() + 180, request_id))
        else:
            if row[channel + '_claim'] != claim:
                return False
            from ..models import now_utc
            column = channel + ('_delivered' if result == 'sent' else '_captured')
            if result in ('sent', 'outbox'):
                conn.execute(f'UPDATE acceptances SET {column}=? WHERE request_id=?', (now_utc().isoformat(), request_id))
            conn.execute(f'UPDATE acceptances SET {channel}_claim=NULL, {channel}_until=NULL WHERE request_id=?', (request_id,))
        return True


async def delivery(request_id, channel, claim, result=None):
    assert channel in ('provider', 'homeowner')
    if supabase_store.enabled():
        return await rpc('quote_acceptance_delivery', dict(p_request=request_id, p_channel=channel, p_claim=claim, p_result=result))
    return await asyncio.to_thread(_delivery, request_id, channel, claim, result)
