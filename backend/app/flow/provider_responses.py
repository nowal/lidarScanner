"""Immutable PDF responses, separate from the mutable conversation record.

One receipt per submission UUID makes retries safe and concurrent providers
independent. Private storage paths never leave the backend; homeowner reads
mint a fresh short-lived URL, including after the email capability expires.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path

from fastapi import HTTPException

from ..config import settings
from ..flow_quotes import ReturnedQuote
from . import supabase_store

MAX_PDF_BYTES = 25 * 1024 * 1024


def _directory(request_id: str) -> Path:
    safe = re.sub(r'[^A-Za-z0-9_-]', '_', request_id)
    path = Path(settings.storage_dir) / 'provider_responses' / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _rows(request_id: str) -> list[dict]:
    if supabase_store.enabled():
        try:
            response = await supabase_store._rest().get('/flow_provider_responses',
                params={'request_id': f'eq.{request_id}', 'select': 'id,fingerprint,quote', 'order': 'created_at.asc,id.asc'})
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise HTTPException(503, 'Quote responses are temporarily unavailable. Please try again.') from exc
    return [json.loads(p.read_text()) for p in sorted(_directory(request_id).glob('*.json'))]


async def responses(request_id: str) -> list[ReturnedQuote]:
    return [ReturnedQuote.model_validate(row['quote']) for row in await _rows(request_id)]


async def existing(request_id: str, submission_id: str, fingerprint: str) -> dict | None:
    row = next((r for r in await _rows(request_id) if r['id'] == submission_id), None)
    if row and row['fingerprint'] != fingerprint:
        raise HTTPException(409, 'This submission was already used for a different quote. Reload to submit a new quote.')
    return row


async def save(request_id: str, submission_id: str, fingerprint: str, quote: ReturnedQuote):
    row = {'id': submission_id, 'request_id': request_id, 'fingerprint': fingerprint,
           'quote': quote.model_dump(mode='json')}
    if supabase_store.enabled():
        try:
            response = await supabase_store._rest().post('/flow_provider_responses',
                headers={'Prefer': 'resolution=ignore-duplicates'}, json=row)
            response.raise_for_status()
        except Exception as exc:
            raise HTTPException(503, 'Your quote could not be saved. Please retry the same submission.') from exc
        if not await existing(request_id, submission_id, fingerprint):
            raise HTTPException(503, 'Your quote could not be confirmed. Please retry.')
    else:
        path = _directory(request_id) / f'{submission_id}.json'
        # Publish a fully flushed receipt atomically; a process crash cannot
        # leave a partial JSON file that poisons every subsequent request read.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, suffix='.tmp', delete=False) as f:
                temporary = Path(f.name)
                json.dump(row, f)
                f.flush()
                os.fsync(f.fileno())
            os.link(temporary, path)
        except FileExistsError:
            await existing(request_id, submission_id, fingerprint)
        finally:
            if temporary: temporary.unlink(missing_ok=True)


async def homeowner_view(record) -> dict:
    result = record.homeowner_view()
    for quote, view in zip(record.quotes, result['quotes']):
        for key in ('document', 'signedDocument'):
            doc = getattr(quote, key)
            if doc and doc.storageBucket and doc.storagePath:
                view[key]['url'] = await supabase_store._signed_storage_url(
                    doc.storageBucket, doc.storagePath, expires_in=300)
    return result


def fingerprint(data: bytes, provider: str, price: float | None, earliest_available_date: date | None = None,
                *, provider_email: str | None = None) -> str:
    details = [provider, price]
    # Preserve retry compatibility with submissions made before the date field.
    if earliest_available_date is not None:
        details.append(earliest_available_date.isoformat())
    if provider_email is not None:
        details.append({'providerEmail': provider_email})
    return hashlib.sha256(data + json.dumps(details, separators=(',', ':')).encode()).hexdigest()
