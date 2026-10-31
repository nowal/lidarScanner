"""Versioned homeowner intake routes; no legacy flow tokens or state machine."""
import asyncio
import base64
import copy
import hashlib
import io
import json
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

import anthropic
from fastapi import APIRouter, Depends, Header, HTTPException
from PIL import Image, ImageOps

from ..config import settings
from ..flow_api import require_token, _scan_upload_owner, DirectQuoteSubmission, submit_direct_quote_request
from ..flow import quote_media, supabase_store
from . import conversation, engine, store, lifecycle
from .schema import TurnRequest, SubmitRequest, CycleRequest

router = APIRouter(prefix=settings.api_prefix + '/ai/quote-intake', dependencies=[Depends(require_token)])


def view(identifier, home, state):
    return dict(conversationId=identifier, homeId=home, revision=state['revision'],
        messages=[{k: t[k] for k in ('id', 'role', 'text', 'attachments') if k in t} for t in state['transcript']],
        result=state['result'], projectZip=state['projectZip'], ready=state.get('ready', False),
        consultationReady=state.get('consultationReady', False),
        paused=state['paused'], submissionPending=state['pendingSubmission'] is not None or bool(state.get('legacySubmissionId') and not state['receipt']),
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


async def store_supplemental_photos(attachments, owner: str, home: str, conversation: str):
    refs = []
    for attachment in attachments:
        try:
            raw = base64.b64decode(attachment.dataBase64, validate=True)
            if not raw or len(raw) > 6_000_000:
                raise ValueError
            with Image.open(io.BytesIO(raw)) as source:
                if (source.format not in ('JPEG', 'PNG', 'HEIF') or source.width < 16
                        or source.height < 16 or source.width * source.height > 40_000_000):
                    raise ValueError
                source.verify()
            with Image.open(io.BytesIO(raw)) as source:
                image = ImageOps.exif_transpose(source).convert('RGB')
                if image.width < 16 or image.height < 16 or image.width * image.height > 40_000_000:
                    raise ValueError
                image.thumbnail((2048, 2048))
                full = io.BytesIO(); image.save(full, format='JPEG', quality=88)
                image.thumbnail((360, 270))
                canvas = Image.new('RGB', (360, 270), '#edf0f3')
                canvas.paste(image, ((360-image.width)//2, (270-image.height)//2))
                thumb = io.BytesIO(); canvas.save(thumb, format='JPEG', quality=82)
        except (ValueError, OSError, base64.binascii.Error, Image.DecompressionBombError):
            raise HTTPException(422, f'{attachment.fileName} is not a valid supported photo')
        digest = hashlib.sha256(full.getvalue()).hexdigest()
        root = f'{owner}/{home}/quote-intake/{conversation}'
        full_path = f'{root}/{attachment.id}-{digest}.jpg'
        thumb_path = f'{root}/{attachment.id}-{digest}-thumb.jpg'
        full_url = await supabase_store.upload_bytes(quote_media.BUCKET, full_path, full.getvalue(), 'image/jpeg')
        thumb_url = await supabase_store.upload_bytes(quote_media.BUCKET, thumb_path, thumb.getvalue(), 'image/jpeg')
        if not full_url or not thumb_url:
            raise HTTPException(503, 'The supplemental photo could not be saved. Please retry.')
        refs.append(dict(id=str(attachment.id), fileName=attachment.fileName, mimeType='image/jpeg',
                         byteCount=len(full.getvalue()), storageBucket=quote_media.BUCKET,
                         storagePath=full_path, thumbnailPath=thumb_path))
    return refs


@router.post('/active')
async def active_intake(body: CycleRequest, x_homeowner_token: str | None = Header(default=None)):
    home = str(body.homeId)
    owner = await _scan_upload_owner(home, x_homeowner_token)
    if body.previousConversationId or body.quoteRequestId:
        raise HTTPException(422, 'Use the new-chat transition for a previous request')
    row = await lifecycle.cycle(owner, home, str(body.conversationId), legacy=body.legacyRequestId, legacy_submission=str(body.legacySubmissionId) if body.legacySubmissionId else None)
    return view(row['id'], home, row['state'])


@router.post('/start-next')
async def start_next(body: CycleRequest, x_homeowner_token: str | None = Header(default=None)):
    home = str(body.homeId)
    owner = await _scan_upload_owner(home, x_homeowner_token)
    if not body.previousConversationId or not body.quoteRequestId or body.legacyRequestId or body.legacySubmissionId:
        raise HTTPException(422, 'Supply the current conversation and accepted request')
    row = await lifecycle.cycle(owner, home, str(body.conversationId),
        previous=str(body.previousConversationId), request=body.quoteRequestId)
    return view(row['id'], home, row['state'])


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
    row = await lifecycle.require_active(owner, home, identifier)
    state, version = copy.deepcopy(row['state']), row['version']
    # Omit empty attachments to preserve identities from the deployed text-only
    # API. Saved fingerprints can recover already accepted photo turns without
    # storing or returning multi-megabyte base64 payloads in conversation JSON.
    request = body.model_dump(mode='json', exclude={'attachments'})
    if body.attachments:
        request['attachments'] = [dict(id=str(a.id), fileName=a.fileName, mimeType=a.mimeType,
                                       sha256=hashlib.sha256(a.dataBase64.encode()).hexdigest()
                                       if a.dataBase64 is not None else a.sha256)
                                  for a in body.attachments]
    turn_id = str(body.clientTurnId)
    if turn_id in state['completed']:
        if state['completed'][turn_id]['request'] != request:
            raise HTTPException(409, 'Turn identity was already used with different content')
        # Latest state avoids rolling a client back if an old retry arrives late.
        return view(identifier, home, state)
    unlocked(state)
    if state['receipt'] or state['pendingSubmission'] or state.get('legacySubmissionId'):
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
        if any(a.dataBase64 is None for a in body.attachments):
            raise HTTPException(422, 'New photos require image bytes, not saved references.')
        ids = [str(a.id) for a in body.attachments]
        known = {photo['id']: photo for photo in state.get('supplementalPhotos', [])}
        if len(set(ids)) != len(ids) or set(ids) & known.keys():
            raise HTTPException(422, 'Each new photo must have a new attachment identity.')
        if len(known) + len(ids) > 12:
            raise HTTPException(422, 'A request supports up to twelve supplemental photos.')
        refs = await store_supplemental_photos(body.attachments, owner, home, identifier) if body.attachments else []
        known.update({photo['id']: photo for photo in refs})
        state['supplementalPhotos'] = list(known.values())
        state['readyBeforePendingTurn'] = state.get('ready', False)
        state['pendingTurn'] = request
        state['transcript'].append(dict(id=turn_id, role='homeowner', text=body.text,
            receivedAt=datetime.now(timezone.utc).isoformat(), timeZone=body.timeZone,
            attachments=[{k: photo[k] for k in ('id','fileName','mimeType','byteCount')} for photo in refs]))
    state['ready'] = False
    state['consultationReady'] = False
    state['leaseUntil'] = time.time() + conversation.MAX_GENERATION_ATTEMPTS * settings.anthropic_request_timeout_seconds + 30
    version = await store.save(identifier, owner, home, state, version)
    try:
        state = await conversation.generate_turn(state, body.timeZone)
        state['revision'] += 1
        state['transcript'].append(dict(id=turn_id + '-reply', role='assistant', text=state['result']['displayText']))
        state['completed'][turn_id] = {'request': request}
        state['pendingTurn'] = None
        state.pop('readyBeforePendingTurn', None)
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
    if state['pendingSubmission'] is None:
        await lifecycle.require_active(owner, home, identifier)
    unlocked(state)
    if state['pendingSubmission'] is None:
        if (body.expectedRevision != state['revision'] or state['pendingTurn'] or state['paused']
                or not (state.get('ready') or state.get('consultationReady')) or not state['projectZip']):
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
        state['consultationReady'] = False
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
