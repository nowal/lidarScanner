"""Durable assistant-only generation and explicitly acknowledged delivery."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field

from ..armor import turn_guard
from ..home_ai import HomeAIChatMessage
from ..scan_update_provider import select_observations
from . import supabase_store
from .scan_update_context import changes, copy_parts, render

logger = logging.getLogger(__name__)


class ScanUpdateRequest(BaseModel):
    threadId: str = Field(min_length=1, max_length=200)
    homeId: UUID
    revision: UUID
    # Only sent after the client atomically saves the bubble + receipt.
    deliveredEventId: UUID | None = None


async def transition(body: ScanUpdateRequest, owner: str, action: str, payload: dict) -> dict:
    if not supabase_store.enabled():
        raise HTTPException(503, detail='Scan update storage is unavailable')
    try:
        response = await supabase_store._rest().post('/rpc/scan_update_transition', json={
            'p_home_id': str(body.homeId), 'p_thread_id': body.threadId,
            'p_revision': str(body.revision), 'p_owner_id': owner,
            'p_action': action, 'p_payload': payload})
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError('Invalid event storage response')
    except Exception as exc:
        logger.warning('Scan update storage unavailable (%s)', type(exc).__name__)
        raise HTTPException(503, detail={'code': 'scan_update_unavailable', 'retryable': True}) from exc
    if code := result.get('error'):
        raise HTTPException(403 if code in ('owner_mismatch', 'thread_home_mismatch') else 409,
                            detail={'code': code, 'retryable': code in (
                                'context_not_ready', 'thread_not_ready', 'event_not_ready', 'lease_lost')})
    return result


def wire(body: ScanUpdateRequest, result: dict) -> dict:
    return {'threadId': body.threadId, 'homeId': str(body.homeId), 'revision': str(body.revision),
            'status': {'skipped': 'none', 'ready': 'ready'}.get(result['status'], 'pending'),
            'eventId': result['eventId'], 'message': result.get('message'),
            'delivered': result.get('deliveredAt') is not None}


async def run(body: ScanUpdateRequest, owner: str) -> dict:
    # Coordinates with ordinary turns on the existing single service. Across
    # workers the DB lease deduplicates generation; zero flow writes means an
    # event cannot overwrite a newer ordinary turn, even on another worker.
    async with turn_guard(body.threadId) as acquired:
        if not acquired:
            raise HTTPException(429, detail='A conversation turn is still in progress')
        if body.deliveredEventId:
            result = await transition(body, owner, 'delivered', {'eventId': str(body.deliveredEventId)})
            return wire(body, result)
        token = str(uuid4())
        result = await transition(body, owner, 'claim', {'token': token})
        if not result.get('claimed'):
            return wire(body, result)
        try:
            delta = changes(result['baselineIndex'], result['contextIndex'])
            if not delta:
                return wire(body, await transition(body, owner, 'skip', {'token': token}))
            intro, evidence = copy_parts(delta)
            # Deadline is shorter than the durable lease. A lost worker cannot
            # later publish over a reclaimed attempt or superseding scan.
            selected = await asyncio.wait_for(select_observations(evidence), timeout=70)
            message = HomeAIChatMessage(id=result['eventId'], role='assistant',
                                        content=render(intro, evidence, selected)).model_dump(mode='json')
            # Retry the SAVE, never the AI call, after an ambiguous DB response.
            for attempt in range(3):
                try:
                    saved = await transition(body, owner, 'finish', {'token': token, 'message': message})
                    return wire(body, saved)
                except HTTPException as exc:
                    if exc.status_code != 503 or attempt == 2:
                        raise
                    await asyncio.sleep(.2 * (attempt + 1))
        except (Exception, asyncio.CancelledError) as exc:
            with contextlib.suppress(Exception):
                await transition(body, owner, 'fail', {'token': token})
            if isinstance(exc, (HTTPException, asyncio.CancelledError)):
                raise
            logger.warning('Scan update generation pending (%s)', type(exc).__name__)
            raise HTTPException(503, detail={'code': 'scan_update_pending', 'retryable': True}) from exc
