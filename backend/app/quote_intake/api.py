"""Versioned homeowner intake routes; no legacy flow tokens or state machine."""
import asyncio
import copy
import json
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

import anthropic
from fastapi import APIRouter, Depends, Header, HTTPException

from ..config import settings
from ..flow_api import require_token, _scan_upload_owner, DirectQuoteSubmission, submit_direct_quote_request
from . import conversation, engine, store
from .schema import TurnRequest, SubmitRequest

router = APIRouter(prefix=settings.api_prefix + '/ai/quote-intake', dependencies=[Depends(require_token)])


def view(identifier, home, state):
    return dict(conversationId=identifier, homeId=home, revision=state['revision'],
        messages=[{k: t[k] for k in ('id', 'role', 'text')} for t in state['transcript']],
        result=state['result'], projectZip=state['projectZip'], ready=state.get('ready', False),
        paused=state['paused'], submissionPending=state['pendingSubmission'] is not None,
        receipt=state['receipt'], pendingTurn=state['pendingTurn'], pendingTurnId=(state['pendingTurn'] or {}).get('clientTurnId'),
        promptVersion=state['promptVersion'], rubricVersion=state['rubric']['rubric_version'])


def unlocked(state):
    if state.get('leaseUntil', 0) > time.time():
        raise HTTPException(409, 'This intake is processing a request. Retry the same turn shortly.')


async def load(identifier, owner, home):
    row = await store.read(identifier, owner, home)
    if row is None:
        raise HTTPException(404, 'Intake not found')
    return row, copy.deepcopy(row['state'])


@router.get('/{conversation_id}')
async def get_intake(conversation_id: UUID, homeId: UUID, x_homeowner_token: str | None = Header(default=None)):
    home = str(homeId)
    owner = await _scan_upload_owner(home, x_homeowner_token)
    _, state = await load(str(conversation_id), owner, home)
    return view(str(conversation_id), home, state)


@router.post('/turn')
async def turn(body: TurnRequest, x_homeowner_token: str | None = Header(default=None)):
    identifier, home = str(body.conversationId), str(body.homeId)
    owner = await _scan_upload_owner(home, x_homeowner_token)
    row = await store.read(identifier, owner, home)
    if row is None:
        if body.expectedRevision != 0:
            raise HTTPException(409, 'Unknown intake revision')
        try:
            state = engine.initial_state()
        except (ValueError, OSError) as exc:
            raise HTTPException(503, 'Quote intake configuration is invalid. Please retry later.') from exc
        version = None
    else:
        state, version = copy.deepcopy(row['state']), row['version']
    request = body.model_dump(mode='json')
    turn_id = str(body.clientTurnId)
    if turn_id in state['completed']:
        if state['completed'][turn_id]['request'] != request:
            raise HTTPException(409, 'Turn identity was already used with different content')
        # Latest state avoids rolling a client back if an old retry arrives late.
        return view(identifier, home, state)
    unlocked(state)
    if state['receipt'] or state['pendingSubmission']:
        raise HTTPException(409, 'This request is already being sent. Recover its receipt before starting a new request.')
    if body.expectedRevision != state['revision']:
        raise HTTPException(409, 'Intake revision changed. Reload the conversation.')
    if state['pendingTurn'] and state['pendingTurn'] != request:
        raise HTTPException(409, 'Retry the saved unfinished turn before sending another message')
    if not body.text.strip():
        raise HTTPException(422, 'Enter a message')
    if state['pendingTurn'] is None:
        if len(state['transcript']) >= 200:
            raise HTTPException(422, 'This conversation is full. Start another request.')
        state['pendingTurn'] = request
        state['transcript'].append(dict(id=turn_id, role='homeowner', text=body.text,
            receivedAt=datetime.now(timezone.utc).isoformat(), timeZone=body.timeZone))
    state['ready'] = False
    state['leaseUntil'] = time.time() + 2 * settings.anthropic_request_timeout_seconds + 30
    version = await store.save(identifier, owner, home, state, version)
    try:
        state = await conversation.generate_turn(state, body.timeZone)
        state['revision'] += 1
        state['transcript'].append(dict(id=turn_id + '-reply', role='assistant', text=state['result']['displayText']))
        state['completed'][turn_id] = {'request': request}
        state['pendingTurn'] = None
        state['leaseUntil'] = 0
        await store.save(identifier, owner, home, state, version)
        return view(identifier, home, state)
    except BaseException as exc:
        # Retain the accepted turn and all preexisting answers on failure.
        saved = await store.read(identifier, owner, home)
        if saved and saved['version'] == version:
            pending = saved['state']
            pending['leaseUntil'] = 0
            await asyncio.shield(store.save(identifier, owner, home, pending, version))
        if isinstance(exc, (HTTPException, asyncio.CancelledError)):
            raise
        code = 429 if isinstance(exc, anthropic.RateLimitError) else 503
        raise HTTPException(code, 'Your message is saved. I couldn’t finish that reply; please retry.') from exc


@router.post('/submit')
async def submit(body: SubmitRequest, x_homeowner_token: str | None = Header(default=None)):
    identifier, home = str(body.conversationId), str(body.homeId)
    owner = await _scan_upload_owner(home, x_homeowner_token)
    row, state = await load(identifier, owner, home)
    if state['receipt']:
        return view(identifier, home, state)
    unlocked(state)
    if state['pendingSubmission'] is None:
        if (body.expectedRevision != state['revision'] or state['pendingTurn'] or state['paused']
                or not state.get('ready') or not state['projectZip']):
            raise HTTPException(409, 'Finish the current scope and project ZIP before submitting')
        package = engine.provider_package(state)
        state['submissionId'] = state['submissionId'] or str(uuid4())
        # No invented room IDs. Whole scan is context; explicit scope remains
        # authoritative in the brief and scopeIntent stays described_scope.
        state['pendingSubmission'] = dict(submissionId=state['submissionId'], homeId=home,
            revision=str(body.uploadRevision), description=package['description'], zip=state['projectZip'],
            intakeDetails=package['details'])
    state['leaseUntil'] = time.time() + 180
    version = await store.save(identifier, owner, home, state, row['version'])
    try:
        response = await submit_direct_quote_request(
            DirectQuoteSubmission.model_validate(state['pendingSubmission']), x_homeowner_token)
        state['receipt'] = json.loads(response.body)
        state['pendingSubmission'] = None
        state['ready'] = False
        state['leaseUntil'] = 0
        await store.save(identifier, owner, home, state, version)
        return view(identifier, home, state)
    except BaseException as exc:
        state['leaseUntil'] = 0
        # Only definitive validation/upload rejection releases queued content.
        # Timeouts, lost success responses and storage failures freeze it.
        if isinstance(exc, HTTPException) and exc.status_code in (400, 403, 422):
            state['pendingSubmission'] = None
        if isinstance(exc, HTTPException) and exc.status_code == 409 and 'still uploading' in str(exc.detail):
            state['pendingSubmission'] = None
        await asyncio.shield(store.save(identifier, owner, home, state, version))
        raise
